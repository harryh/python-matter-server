"""Matter Device Controller implementation.

This module implements the Matter Device Controller WebSocket API. Compared to the
`ChipDeviceControllerWrapper` class it adds the WebSocket specific sauce and adds more
features which are not part of the Python Matter Device Controller per-se, e.g.
pinging a device.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import logging
from random import randint
import time
from typing import TYPE_CHECKING, Any, cast

from chip.clusters import Attribute, Objects as Clusters
from chip.clusters.Attribute import ValueDecodeFailure
from chip.clusters.ClusterObjects import ALL_ATTRIBUTES, ALL_CLUSTERS, Cluster
from chip.discovery import DiscoveryType
from chip.exceptions import ChipStackError
from zeroconf import BadTypeInNameException, IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from matter_server.common.const import VERBOSE_LOG_LEVEL
from matter_server.common.custom_clusters import check_polled_attributes
from matter_server.common.models import CommissionableNodeData, CommissioningParameters
from matter_server.server.helpers.attributes import parse_attributes_from_read_result
from matter_server.server.helpers.utils import ping_ip
from matter_server.server.sdk import ChipDeviceControllerWrapper

from ..common.errors import (
    InvalidArguments,
    NodeCommissionFailed,
    NodeInterviewFailed,
    NodeNotExists,
    NodeNotReady,
    NodeNotResolving,
)
from ..common.helpers.api import api_command
from ..common.helpers.json import JSON_DECODE_EXCEPTIONS, json_loads
from ..common.helpers.util import (
    create_attribute_path_from_attribute,
    dataclass_from_dict,
    parse_attribute_path,
    parse_value,
)
from ..common.models import (
    APICommand,
    EventType,
    MatterNodeData,
    MatterNodeEvent,
    NodePingResult,
)
from .const import DATA_MODEL_SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from chip.native import PyChipError

    from .server import MatterServer

DATA_KEY_NODES = "nodes"
DATA_KEY_LAST_NODE_ID = "last_node_id"

LOGGER = logging.getLogger(__name__)
NODE_SUBSCRIPTION_CEILING_WIFI = 60
NODE_SUBSCRIPTION_CEILING_THREAD = 60
NODE_SUBSCRIPTION_CEILING_BATTERY_POWERED = 600
MAX_COMMISSION_RETRIES = 3
NODE_RESUBSCRIBE_ATTEMPTS_UNAVAILABLE = 3
NODE_RESUBSCRIBE_TIMEOUT_OFFLINE = 30 * 60 * 1000
NODE_PING_TIMEOUT = 10
NODE_PING_TIMEOUT_BATTERY_POWERED = 60
NODE_MDNS_BACKOFF = 610  # must be higher than (highest) sub ceiling
FALLBACK_NODE_SCANNER_INTERVAL = 1800
CUSTOM_ATTRIBUTES_POLLER_INTERVAL = 30

MDNS_TYPE_OPERATIONAL_NODE = "_matter._tcp.local."
MDNS_TYPE_COMMISSIONABLE_NODE = "_matterc._udp.local."

TEST_NODE_START = 900000

ROUTING_ROLE_ATTRIBUTE_PATH = create_attribute_path_from_attribute(
    0, Clusters.ThreadNetworkDiagnostics.Attributes.RoutingRole
)
DESCRIPTOR_PARTS_LIST_ATTRIBUTE_PATH = create_attribute_path_from_attribute(
    0, Clusters.Descriptor.Attributes.PartsList
)
BASIC_INFORMATION_SOFTWARE_VERSION_ATTRIBUTE_PATH = (
    create_attribute_path_from_attribute(
        0, Clusters.BasicInformation.Attributes.SoftwareVersion
    )
)

# pylint: disable=too-many-lines,too-many-instance-attributes,too-many-public-methods


class MatterDeviceController:
    """Class that manages the Matter devices."""

    def __init__(
        self,
        server: MatterServer,
        paa_root_cert_dir: Path,
    ):
        """Initialize the device controller."""
        self.server = server

        self._chip_device_controller = ChipDeviceControllerWrapper(
            server, paa_root_cert_dir
        )

        # we keep the last events in memory so we can include them in the diagnostics dump
        self.event_history: deque[Attribute.EventReadResult] = deque(maxlen=25)
        self._compressed_fabric_id: int | None = None
        self._fabric_id_hex: str | None = None
        self._wifi_credentials_set: bool = False
        self._thread_credentials_set: bool = False
        self._nodes_in_setup: set[int] = set()
        self._node_last_seen: dict[int, float] = {}
        self._nodes: dict[int, MatterNodeData] = {}
        self._last_known_ip_addresses: dict[int, list[str]] = {}
        self._last_subscription_attempt: dict[int, int] = {}
        self._known_commissioning_params: dict[int, CommissioningParameters] = {}
        self._aiobrowser: AsyncServiceBrowser | None = None
        self._aiozc: AsyncZeroconf | None = None
        self._fallback_node_scanner_timer: asyncio.TimerHandle | None = None
        self._fallback_node_scanner_task: asyncio.Task | None = None
        self._node_setup_throttle = asyncio.Semaphore(5)
        self._mdns_event_timer: dict[str, asyncio.TimerHandle] = {}
        self._polled_attributes: dict[int, set[str]] = {}
        self._custom_attribute_poller_timer: asyncio.TimerHandle | None = None
        self._custom_attribute_poller_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """Initialize the device controller."""
        self._compressed_fabric_id = (
            await self._chip_device_controller.get_compressed_fabric_id()
        )
        self._fabric_id_hex = hex(self._compressed_fabric_id)[2:]

    async def start(self) -> None:
        """Handle logic on controller start."""
        # load nodes from persistent storage
        nodes: dict[str, dict | None] = self.server.storage.get(DATA_KEY_NODES, {})
        orphaned_nodes: set[str] = set()
        for node_id_str, node_dict in nodes.items():
            node_id = int(node_id_str)
            if node_dict is None:
                # Non-initialized (left-over) node from a failed commissioning attempt.
                # NOTE: This code can be removed in a future version
                # as this can no longer happen.
                orphaned_nodes.add(node_id_str)
                continue
            try:
                node = dataclass_from_dict(MatterNodeData, node_dict, strict=True)
            except (KeyError, ValueError):
                # constructing MatterNodeData from the cached dict is not possible,
                # revert to a fallback object and the node will be re-interviewed
                node = MatterNodeData(
                    node_id=node_id,
                    date_commissioned=node_dict.get(
                        "date_commissioned",
                        datetime(1970, 1, 1),
                    ),
                    last_interview=node_dict.get(
                        "last_interview",
                        datetime(1970, 1, 1),
                    ),
                    interview_version=0,
                )
            # always mark node as unavailable at startup until subscriptions are ready
            node.available = False
            self._nodes[node_id] = node
        # cleanup orhpaned nodes from storage
        for node_id_str in orphaned_nodes:
            self.server.storage.remove(DATA_KEY_NODES, node_id_str)
        LOGGER.info("Loaded %s nodes from stored configuration", len(self._nodes))
        # set-up mdns browser
        self._aiozc = AsyncZeroconf(ip_version=IPVersion.All)
        services = [MDNS_TYPE_OPERATIONAL_NODE, MDNS_TYPE_COMMISSIONABLE_NODE]
        self._aiobrowser = AsyncServiceBrowser(
            self._aiozc.zeroconf,
            services,
            handlers=[self._on_mdns_service_state_change],
        )
        # set-up fallback node scanner
        self._schedule_fallback_scanner()

    async def stop(self) -> None:
        """Handle logic on server stop."""
        # shutdown (and cleanup) mdns browser and fallback node scanner
        if self._aiobrowser:
            await self._aiobrowser.async_cancel()
        if self._fallback_node_scanner_timer:
            self._fallback_node_scanner_timer.cancel()
        if (scan_task := self._fallback_node_scanner_task) and not scan_task.done():
            scan_task.cancel()
        if self._aiozc:
            await self._aiozc.async_close()

        # shutdown the sdk device controller
        await self._chip_device_controller.shutdown()
        LOGGER.debug("Stopped.")

    @property
    def compressed_fabric_id(self) -> int | None:
        """Return the compressed fabric id."""
        return self._compressed_fabric_id

    @property
    def wifi_credentials_set(self) -> bool:
        """Return if WiFi credentials have been set."""
        return self._wifi_credentials_set

    @property
    def thread_credentials_set(self) -> bool:
        """Return if Thread operational dataset as been set."""
        return self._thread_credentials_set

    @api_command(APICommand.GET_NODES)
    def get_nodes(self, only_available: bool = False) -> list[MatterNodeData]:
        """Return all Nodes known to the server."""
        return [
            x
            for x in self._nodes.values()
            if x is not None and (x.available or not only_available)
        ]

    @api_command(APICommand.GET_NODE)
    def get_node(self, node_id: int) -> MatterNodeData:
        """Return info of a single Node."""
        if node := self._nodes.get(node_id):
            return node
        raise NodeNotExists(f"Node {node_id} does not exist or is not yet interviewed")

    @api_command(APICommand.COMMISSION_WITH_CODE)
    async def commission_with_code(
        self, code: str, network_only: bool = False
    ) -> MatterNodeData:
        """
        Commission a device using a QR Code or Manual Pairing Code.

        :param code: The QR Code or Manual Pairing Code for device commissioning.
        :param network_only: If True, restricts device discovery to network only.

        :return: The NodeInfo of the commissioned device.
        """
        node_id = self._get_next_node_id()

        attempts = 0
        # we retry commissioning a few times as we've seen devices in the wild
        # that are a bit unstable.
        # by retrying, we increase the chances of a successful commission
        while attempts <= MAX_COMMISSION_RETRIES:
            attempts += 1
            LOGGER.info(
                "Starting Matter commissioning with code using Node ID %s (attempt %s/%s).",
                node_id,
                attempts,
                MAX_COMMISSION_RETRIES,
            )
            result: (
                PyChipError | None
            ) = await self._chip_device_controller.commission_with_code(
                node_id,
                code,
                DiscoveryType.DISCOVERY_NETWORK_ONLY
                if network_only
                else DiscoveryType.DISCOVERY_ALL,
            )
            if result and result.is_success:
                break
            if attempts >= MAX_COMMISSION_RETRIES:
                raise NodeCommissionFailed(
                    f"Commission with code failed for node {node_id}."
                )
            await asyncio.sleep(5)

        LOGGER.info("Matter commissioning of Node ID %s successful.", node_id)

        # perform full (first) interview of the device
        # we retry the interview max 3 times as it may fail in noisy
        # RF environments (in case of thread), mdns trouble or just flaky devices.
        # retrying both the mdns resolve and (first) interview, increases the chances
        # of a successful device commission.
        retries = 3
        while retries:
            try:
                await self.interview_node(node_id)
            except (NodeNotResolving, NodeInterviewFailed) as err:
                if retries <= 0:
                    raise err
                retries -= 1
                LOGGER.warning("Unable to interview Node %s: %s", node_id, err)
                await asyncio.sleep(5)
            else:
                break

        # make sure we start a subscription for this newly added node
        await self._setup_node(node_id)
        LOGGER.info("Commissioning of Node ID %s completed.", node_id)
        # return full node object once we're complete
        return self.get_node(node_id)

    @api_command(APICommand.COMMISSION_ON_NETWORK)
    async def commission_on_network(
        self,
        setup_pin_code: int,
        filter_type: int = 0,
        filter: Any = None,  # pylint: disable=redefined-builtin
        ip_addr: str | None = None,
    ) -> MatterNodeData:
        """
        Do the routine for OnNetworkCommissioning, with a filter for mDNS discovery.

        The filter can be an integer,
        a string or None depending on the actual type of selected filter.

        NOTE: For advanced usecases only, use `commission_with_code`
        for regular commissioning.

        Returns full NodeInfo once complete.
        """
        node_id = self._get_next_node_id()
        if ip_addr is not None:
            ip_addr = self.server.scope_ipv6_lla(ip_addr)

        attempts = 0
        # we retry commissioning a few times as we've seen devices in the wild
        # that are a bit unstable.
        # by retrying, we increase the chances of a successful commission
        while attempts <= MAX_COMMISSION_RETRIES:
            attempts += 1
            result: PyChipError | None
            if ip_addr is None:
                # regular CommissionOnNetwork if no IP address provided
                LOGGER.info(
                    "Starting Matter commissioning on network using Node ID %s (attempt %s/%s).",
                    node_id,
                    attempts,
                    MAX_COMMISSION_RETRIES,
                )
                result = await self._chip_device_controller.commission_on_network(
                    node_id, setup_pin_code, filter_type, filter
                )
            else:
                LOGGER.info(
                    "Starting Matter commissioning using Node ID %s and IP %s (attempt %s/%s).",
                    node_id,
                    ip_addr,
                    attempts,
                    MAX_COMMISSION_RETRIES,
                )
                result = await self._chip_device_controller.commission_ip(
                    node_id, setup_pin_code, ip_addr
                )
            if result and result.is_success:
                break
            if attempts >= MAX_COMMISSION_RETRIES:
                raise NodeCommissionFailed(f"Commissioning failed for node {node_id}.")
            await asyncio.sleep(5)

        LOGGER.info("Matter commissioning of Node ID %s successful.", node_id)

        # perform full (first) interview of the device
        # we retry the interview max 3 times as it may fail in noisy
        # RF environments (in case of thread), mdns trouble or just flaky devices.
        # retrying both the mdns resolve and (first) interview, increases the chances
        # of a successful device commission.
        retries = 3
        while retries:
            try:
                await self.interview_node(node_id)
            except NodeInterviewFailed as err:
                if retries <= 0:
                    raise err
                retries -= 1
                LOGGER.warning("Unable to interview Node %s: %s", node_id, err)
                await asyncio.sleep(5)
            else:
                break
        # make sure we start a subscription for this newly added node
        await self._setup_node(node_id)
        LOGGER.info("Commissioning of Node ID %s completed.", node_id)
        # return full node object once we're complete
        return self.get_node(node_id)

    @api_command(APICommand.SET_WIFI_CREDENTIALS)
    async def set_wifi_credentials(self, ssid: str, credentials: str) -> None:
        """Set WiFi credentials for commissioning to a (new) device."""

        await self._chip_device_controller.set_wifi_credentials(ssid, credentials)
        self._wifi_credentials_set = True

    @api_command(APICommand.SET_THREAD_DATASET)
    async def set_thread_operational_dataset(self, dataset: str) -> None:
        """Set Thread Operational dataset in the stack."""

        await self._chip_device_controller.set_thread_operational_dataset(dataset)
        self._thread_credentials_set = True

    @api_command(APICommand.OPEN_COMMISSIONING_WINDOW)
    async def open_commissioning_window(
        self,
        node_id: int,
        timeout: int = 300,
        iteration: int = 1000,
        option: int = 1,
        discriminator: int | None = None,
    ) -> CommissioningParameters:
        """Open a commissioning window to commission a device present on this controller to another.

        Returns code to use as discriminator.
        """
        if (node := self._nodes.get(node_id)) is None or not node.available:
            raise NodeNotReady(f"Node {node_id} is not (yet) available.")

        if node_id in self._known_commissioning_params:
            # node has already been put into commissioning mode,
            # return previous parameters
            return self._known_commissioning_params[node_id]

        if discriminator is None:
            discriminator = randint(0, 4095)  # noqa: S311

        sdk_result = await self._chip_device_controller.open_commissioning_window(
            node_id,
            timeout,
            iteration,
            discriminator,
            option,
        )
        self._known_commissioning_params[node_id] = params = CommissioningParameters(
            setup_pin_code=sdk_result.setupPinCode,
            setup_manual_code=sdk_result.setupManualCode,
            setup_qr_code=sdk_result.setupQRCode,
        )
        # we store the commission parameters and clear them after the timeout
        if TYPE_CHECKING:
            assert self.server.loop
        self.server.loop.call_later(
            timeout, self._known_commissioning_params.pop, node_id, None
        )
        return params

    @api_command(APICommand.DISCOVER)
    async def discover_commissionable_nodes(
        self,
    ) -> list[CommissionableNodeData]:
        """Discover Commissionable Nodes (discovered on BLE or mDNS)."""
        sdk_result = await self._chip_device_controller.discover_commissionable_nodes()
        if sdk_result is None:
            return []
        # ensure list
        if not isinstance(sdk_result, list):
            sdk_result = [sdk_result]
        return [
            CommissionableNodeData(
                instance_name=x.instanceName,
                host_name=x.hostName,
                port=x.port,
                long_discriminator=x.longDiscriminator,
                vendor_id=x.vendorId,
                product_id=x.productId,
                commissioning_mode=x.commissioningMode,
                device_type=x.deviceType,
                device_name=x.deviceName,
                pairing_instruction=x.pairingInstruction,
                pairing_hint=x.pairingHint,
                mrp_retry_interval_idle=x.mrpRetryIntervalIdle,
                mrp_retry_interval_active=x.mrpRetryIntervalActive,
                supports_tcp=x.supportsTcp,
                addresses=x.addresses,
                rotating_id=x.rotatingId,
            )
            for x in sdk_result
        ]

    @api_command(APICommand.INTERVIEW_NODE)
    async def interview_node(self, node_id: int) -> None:
        """Interview a node."""
        if node_id >= TEST_NODE_START:
            LOGGER.debug(
                "interview_node called for test node %s",
                node_id,
            )
            self.server.signal_event(EventType.NODE_UPDATED, self._nodes[node_id])
            return

        try:
            LOGGER.info("Interviewing node: %s", node_id)
            read_response: Attribute.AsyncReadTransaction.ReadResponse = (
                await self._chip_device_controller.read_attribute(
                    node_id,
                    [()],
                    fabric_filtered=False,
                )
            )
        except ChipStackError as err:
            raise NodeInterviewFailed(f"Failed to interview node {node_id}") from err

        is_new_node = node_id not in self._nodes
        existing_info = self._nodes.get(node_id)
        node = MatterNodeData(
            node_id=node_id,
            date_commissioned=(
                existing_info.date_commissioned if existing_info else datetime.utcnow()
            ),
            last_interview=datetime.utcnow(),
            interview_version=DATA_MODEL_SCHEMA_VERSION,
            available=existing_info.available if existing_info else False,
            attributes=parse_attributes_from_read_result(read_response.tlvAttributes),
        )

        if existing_info:
            node.attribute_subscriptions = existing_info.attribute_subscriptions
        # work out if the node is a bridge device by looking at the devicetype of endpoint 1
        if attr_data := node.attributes.get("1/29/0"):
            node.is_bridge = any(x[0] == 14 for x in attr_data)

        # save updated node data
        self._nodes[node_id] = node
        self._write_node_state(node_id, True)
        if is_new_node:
            # new node - first interview
            self.server.signal_event(EventType.NODE_ADDED, node)
        else:
            # existing node, signal node updated event
            # TODO: maybe only signal this event if attributes actually changed ?
            self.server.signal_event(EventType.NODE_UPDATED, node)

        LOGGER.debug("Interview of node %s completed", node_id)

    @api_command(APICommand.DEVICE_COMMAND)
    async def send_device_command(
        self,
        node_id: int,
        endpoint_id: int,
        cluster_id: int,
        command_name: str,
        payload: dict,
        response_type: Any | None = None,
        timed_request_timeout_ms: int | None = None,
        interaction_timeout_ms: int | None = None,
    ) -> Any:
        """Send a command to a Matter node/device."""
        if (node := self._nodes.get(node_id)) is None or not node.available:
            raise NodeNotReady(f"Node {node_id} is not (yet) available.")
        cluster_cls: Cluster = ALL_CLUSTERS[cluster_id]
        command_cls = getattr(cluster_cls.Commands, command_name)
        command = dataclass_from_dict(command_cls, payload, allow_sdk_types=True)
        if node_id >= TEST_NODE_START:
            LOGGER.debug(
                "send_device_command called for test node %s on endpoint_id: %s - "
                "cluster_id: %s - command_name: %s - payload: %s\n%s",
                node_id,
                endpoint_id,
                cluster_id,
                command_name,
                payload,
                command,
            )
            return None
        return await self._chip_device_controller.send_command(
            node_id,
            endpoint_id,
            command,
            response_type,
            timed_request_timeout_ms,
            interaction_timeout_ms,
        )

    @api_command(APICommand.READ_ATTRIBUTE)
    async def read_attribute(
        self,
        node_id: int,
        attribute_path: str | list[str],
        fabric_filtered: bool = False,
    ) -> dict[str, Any]:
        """
        Read one or more attribute(s) on a node by specifying an attributepath.

        The attribute path can be a single string or a list of strings.
        The attribute path may contain wildcards (*) for cluster and/or attribute id.

        The return type is a dictionary with the attribute path as key and the value as value.
        """
        if (node := self._nodes.get(node_id)) is None or not node.available:
            raise NodeNotReady(f"Node {node_id} is not (yet) available.")
        attribute_paths = (
            attribute_path if isinstance(attribute_path, list) else [attribute_path]
        )

        # handle test node
        if node_id >= TEST_NODE_START:
            LOGGER.debug(
                "read_attribute called for test node %s on path(s): %s - fabric_filtered: %s",
                node_id,
                str(attribute_paths),
                fabric_filtered,
            )
            return {
                attr_path: self._nodes[node_id].attributes.get(attr_path)
                for attr_path in attribute_paths
            }

        # parse text based attribute paths into the SDK Attribute Path objects
        attributes: list[Attribute.AttributePath] = []
        for attr_path in attribute_paths:
            endpoint_id, cluster_id, attribute_id = parse_attribute_path(attr_path)
            attributes.append(
                Attribute.AttributePath(
                    EndpointId=endpoint_id,
                    ClusterId=cluster_id,
                    AttributeId=attribute_id,
                )
            )

        result = await self._chip_device_controller.read(
            node_id,
            attributes,
            fabric_filtered,
        )
        read_atributes = parse_attributes_from_read_result(result.tlvAttributes)
        # update cached info in node attributes and signal events for updated attributes
        values_changed = False
        for attr_path, value in read_atributes.items():
            if node.attributes.get(attr_path) != value:
                node.attributes[attr_path] = value
                self.server.signal_event(
                    EventType.ATTRIBUTE_UPDATED,
                    # send data as tuple[node_id, attribute_path, new_value]
                    (node_id, attr_path, value),
                )

                values_changed = True
        # schedule writing of the node state if any values changed
        if values_changed:
            self._write_node_state(node_id)
        return read_atributes

    @api_command(APICommand.WRITE_ATTRIBUTE)
    async def write_attribute(
        self,
        node_id: int,
        attribute_path: str,
        value: Any,
    ) -> Any:
        """Write an attribute(value) on a target node."""
        if (node := self._nodes.get(node_id)) is None or not node.available:
            raise NodeNotReady(f"Node {node_id} is not (yet) available.")
        endpoint_id, cluster_id, attribute_id = parse_attribute_path(attribute_path)
        if endpoint_id is None:
            raise InvalidArguments(f"Invalid attribute path: {attribute_path}")
        attribute = cast(
            Clusters.ClusterAttributeDescriptor,
            ALL_ATTRIBUTES[cluster_id][attribute_id](),
        )
        attribute.value = parse_value(
            name=attribute_path,
            value=value,
            value_type=attribute.attribute_type.Type,
            allow_sdk_types=True,
        )
        if node_id >= TEST_NODE_START:
            LOGGER.debug(
                "write_attribute called for test node %s on path %s - value %s\n%s",
                node_id,
                attribute_path,
                value,
                attribute,
            )
            return None
        return await self._chip_device_controller.write_attribute(
            node_id, [(endpoint_id, attribute)]
        )

    @api_command(APICommand.REMOVE_NODE)
    async def remove_node(self, node_id: int) -> None:
        """Remove a Matter node/device from the fabric."""
        if node_id not in self._nodes:
            raise NodeNotExists(
                f"Node {node_id} does not exist or has not been interviewed."
            )

        LOGGER.info("Removing Node ID %s.", node_id)

        # shutdown any existing subscriptions
        await self._chip_device_controller.shutdown_subscription(node_id)
        self._polled_attributes.pop(node_id, None)

        node = self._nodes.pop(node_id)
        self.server.storage.remove(
            DATA_KEY_NODES,
            subkey=str(node_id),
        )

        LOGGER.info("Node ID %s successfully removed from Matter server.", node_id)

        self.server.signal_event(EventType.NODE_REMOVED, node_id)

        if node is None or node_id >= TEST_NODE_START:
            return

        attribute_path = create_attribute_path_from_attribute(
            0,
            Clusters.OperationalCredentials.Attributes.CurrentFabricIndex,
        )
        fabric_index = node.attributes.get(attribute_path)
        if fabric_index is None:
            return
        result: Clusters.OperationalCredentials.Commands.NOCResponse | None = None
        try:
            result = await self._chip_device_controller.send_command(
                node_id=node_id,
                endpoint_id=0,
                command=Clusters.OperationalCredentials.Commands.RemoveFabric(
                    fabricIndex=fabric_index,
                ),
            )
        except ChipStackError as err:
            LOGGER.warning(
                "Removing current fabric from device failed: %s",
                str(err) or err.__class__.__name__,
                # only log stacktrace if we have verbose logging enabled
                exc_info=err if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else None,
            )
            return
        if (
            result is None
            or result.statusCode
            == Clusters.OperationalCredentials.Enums.NodeOperationalCertStatusEnum.kOk
        ):
            LOGGER.info("Successfully removed Home Assistant fabric from device.")
        else:
            LOGGER.warning(
                "Removing current fabric from device failed with status code %d.",
                result.statusCode,
            )

    @api_command(APICommand.PING_NODE)
    async def ping_node(self, node_id: int, attempts: int = 1) -> NodePingResult:
        """Ping node on the currently known IP-adress(es)."""
        result: NodePingResult = {}
        if node_id >= TEST_NODE_START:
            return {"0.0.0.0": True, "0000:1111:2222:3333:4444": True}
        node = self._nodes.get(node_id)
        if node is None:
            raise NodeNotExists(
                f"Node {node_id} does not exist or is not yet interviewed"
            )
        node_logger = LOGGER.getChild(f"node_{node_id}")

        battery_powered = (
            node.attributes.get(ROUTING_ROLE_ATTRIBUTE_PATH, 0)
            == Clusters.ThreadNetworkDiagnostics.Enums.RoutingRoleEnum.kSleepyEndDevice
        )

        async def _do_ping(ip_address: str) -> None:
            """Ping IP and add to result."""
            timeout = (
                NODE_PING_TIMEOUT_BATTERY_POWERED
                if battery_powered
                else NODE_PING_TIMEOUT
            )
            if "%" in ip_address:
                # ip address contains an interface index
                clean_ip, interface_idx = ip_address.split("%", 1)
                node_logger.debug(
                    "Pinging address %s (using interface %s)", clean_ip, interface_idx
                )
            else:
                clean_ip = ip_address
                node_logger.debug("Pinging address %s", clean_ip)
            result[clean_ip] = await ping_ip(ip_address, timeout, attempts=attempts)

        ip_addresses = await self.get_node_ip_addresses(
            node_id, prefer_cache=False, scoped=True
        )
        tasks = [_do_ping(x) for x in ip_addresses]
        # TODO: replace this gather with a taskgroup once we bump our py version
        await asyncio.gather(*tasks)

        # retrieve the currently connected/used address which is used
        # by the sdk for communicating with the device
        if sdk_result := await self._chip_device_controller.get_address_and_port(
            node_id
        ):
            active_address = sdk_result[0]
            node_logger.info(
                "The SDK is communicating with the device using %s", active_address
            )
            if active_address not in result and node.available:
                # if the sdk is connected to a node, treat the address as pingable
                result[active_address] = True

        return result

    @api_command(APICommand.GET_NODE_IP_ADRESSES)
    async def get_node_ip_addresses(
        self,
        node_id: int,
        prefer_cache: bool = False,
        scoped: bool = False,
    ) -> list[str]:
        """Return the currently known (scoped) IP-adress(es)."""
        cached_info = self._last_known_ip_addresses.get(node_id, [])
        if prefer_cache and cached_info:
            return cached_info if scoped else [x.split("%")[0] for x in cached_info]
        node = self._nodes.get(node_id)
        if node is None:
            raise NodeNotExists(
                f"Node {node_id} does not exist or is not yet interviewed"
            )
        node_logger = LOGGER.getChild(f"node_{node_id}")
        # query mdns for all IP's
        # ensure both fabric id and node id have 16 characters (prefix with zero's)
        mdns_name = f"{self.compressed_fabric_id:0{16}X}-{node_id:0{16}X}.{MDNS_TYPE_OPERATIONAL_NODE}"
        info = AsyncServiceInfo(MDNS_TYPE_OPERATIONAL_NODE, mdns_name)
        if TYPE_CHECKING:
            assert self._aiozc is not None
        if not await info.async_request(self._aiozc.zeroconf, 3000):
            node_logger.info(
                "Node could not be discovered on the network, returning cached IP's"
            )
            return cached_info
        ip_adresses = info.parsed_scoped_addresses(IPVersion.All)
        # cache this info for later use
        self._last_known_ip_addresses[node_id] = ip_adresses
        return ip_adresses if scoped else [x.split("%")[0] for x in ip_adresses]

    @api_command(APICommand.IMPORT_TEST_NODE)
    async def import_test_node(self, dump: str) -> None:
        """Import test node(s) from a HA or Matter server diagnostics dump."""
        try:
            dump_data = cast(dict, json_loads(dump))
        except JSON_DECODE_EXCEPTIONS as err:
            raise InvalidArguments("Invalid json") from err
        # the dump format we accept here is a Home Assistant diagnostics file
        # dump can either be a single dump or a full dump with multiple nodes
        dump_nodes: list[dict[str, Any]]
        if "node" in dump_data["data"]:
            dump_nodes = [dump_data["data"]["node"]]
        else:
            dump_nodes = dump_data["data"]["server"]["nodes"]
        # node ids > 900000 are reserved for test nodes
        next_test_node_id = max(*(x for x in self._nodes), TEST_NODE_START) + 1
        for node_dict in dump_nodes:
            node = dataclass_from_dict(MatterNodeData, node_dict, strict=True)
            node.node_id = next_test_node_id
            next_test_node_id += 1
            self._nodes[node.node_id] = node
            self.server.signal_event(EventType.NODE_ADDED, node)

    async def _subscribe_node(self, node_id: int) -> None:
        """
        Subscribe to all node state changes/events for an individual node.

        Note that by using the listen command at server level,
        you will receive all (subscribed) node events and attribute updates.
        """
        # pylint: disable=too-many-locals,too-many-statements
        if self._nodes.get(node_id) is None:
            raise NodeNotExists(
                f"Node {node_id} does not exist or has not been interviewed."
            )

        node_logger = LOGGER.getChild(f"node_{node_id}")
        node = self._nodes[node_id]

        # Shutdown existing subscriptions for this node first
        await self._chip_device_controller.shutdown_subscription(node_id)

        loop = cast(asyncio.AbstractEventLoop, self.server.loop)

        def attribute_updated(
            path: Attribute.AttributePath,
            old_value: Any,
            new_value: Any,
        ) -> None:
            node_logger.log(
                VERBOSE_LOG_LEVEL,
                "Attribute updated: %s - old value: %s - new value: %s",
                path,
                old_value,
                new_value,
            )

            # work out added/removed endpoints on bridges
            if node.is_bridge and str(path) == DESCRIPTOR_PARTS_LIST_ATTRIBUTE_PATH:
                endpoints_removed = set(old_value or []) - set(new_value)
                endpoints_added = set(new_value) - set(old_value or [])
                if endpoints_removed:
                    self._handle_endpoints_removed(node_id, endpoints_removed)
                if endpoints_added:
                    loop.create_task(
                        self._handle_endpoints_added(node_id, endpoints_added)
                    )
                return

            # work out if software version changed
            if (
                str(path) == BASIC_INFORMATION_SOFTWARE_VERSION_ATTRIBUTE_PATH
                and new_value != old_value
            ):
                # schedule a full interview of the node if the software version changed
                loop.create_task(self.interview_node(node_id))

            # store updated value in node attributes
            node.attributes[str(path)] = new_value

            # schedule save to persistent storage
            self._write_node_state(node_id)

            # This callback is running in the CHIP stack thread
            self.server.signal_event(
                EventType.ATTRIBUTE_UPDATED,
                # send data as tuple[node_id, attribute_path, new_value]
                (node_id, str(path), new_value),
            )

        def attribute_updated_callback(
            path: Attribute.AttributePath,
            transaction: Attribute.SubscriptionTransaction,
        ) -> None:
            self._node_last_seen[node_id] = time.time()
            new_value = transaction.GetTLVAttribute(path)
            # failsafe: ignore ValueDecodeErrors
            # these are set by the SDK if parsing the value failed miserably
            if isinstance(new_value, ValueDecodeFailure):
                return

            old_value = node.attributes.get(str(path))

            # return early if the value did not actually change at all
            if old_value == new_value:
                return

            loop.call_soon_threadsafe(attribute_updated, path, old_value, new_value)

        def event_callback(
            data: Attribute.EventReadResult,
            transaction: Attribute.SubscriptionTransaction,
        ) -> None:
            # pylint: disable=unused-argument
            assert loop is not None
            node_logger.log(
                VERBOSE_LOG_LEVEL,
                "Received node event: %s - transaction: %s",
                data,
                transaction,
            )
            self._node_last_seen[node_id] = time.time()
            node_event = MatterNodeEvent(
                node_id=node_id,
                endpoint_id=data.Header.EndpointId,
                cluster_id=data.Header.ClusterId,
                event_id=data.Header.EventId,
                event_number=data.Header.EventNumber,
                priority=data.Header.Priority,
                timestamp=data.Header.Timestamp,
                timestamp_type=data.Header.TimestampType,
                data=data.Data,
            )
            self.event_history.append(node_event)
            loop.call_soon_threadsafe(
                self.server.signal_event, EventType.NODE_EVENT, node_event
            )

        def error_callback(
            chipError: int, transaction: Attribute.SubscriptionTransaction
        ) -> None:
            # pylint: disable=unused-argument, invalid-name
            node_logger.error("Got error from node: %s", chipError)

        def resubscription_attempted(
            transaction: Attribute.SubscriptionTransaction,
            terminationError: int,
            nextResubscribeIntervalMsec: int,
        ) -> None:
            # pylint: disable=unused-argument, invalid-name
            node_logger.info(
                "Previous subscription failed with Error: %s, re-subscribing in %s ms...",
                terminationError,
                nextResubscribeIntervalMsec,
            )
            resubscription_attempt = self._last_subscription_attempt[node_id] + 1
            self._last_subscription_attempt[node_id] = resubscription_attempt
            # Mark node as unavailable and signal consumers.
            # We debounce it a bit so we only mark the node unavailable
            # after some resubscription attempts and we shutdown the subscription
            # if the resubscription interval exceeds 30 minutes (TTL of mdns).
            # The node will be auto picked up by mdns if it's alive again.
            if (
                node.available
                and resubscription_attempt >= NODE_RESUBSCRIBE_ATTEMPTS_UNAVAILABLE
            ):
                node.available = False
                self.server.signal_event(EventType.NODE_UPDATED, node)
                LOGGER.info("Marked node %s as unavailable", node_id)
            if (
                not node.available
                and nextResubscribeIntervalMsec > NODE_RESUBSCRIBE_TIMEOUT_OFFLINE
            ):
                asyncio.create_task(self._node_offline(node_id))

        def resubscription_succeeded(
            transaction: Attribute.SubscriptionTransaction,
        ) -> None:
            # pylint: disable=unused-argument, invalid-name
            self._node_last_seen[node_id] = time.time()
            node_logger.info("Re-Subscription succeeded")
            self._last_subscription_attempt[node_id] = 0
            # mark node as available and signal consumers
            if not node.available:
                node.available = True
                self.server.signal_event(EventType.NODE_UPDATED, node)

        node_logger.info("Setting up attributes and events subscription.")
        interval_floor = 0
        # determine subscription ceiling based on routing role
        # Endpoint 0, ThreadNetworkDiagnostics Cluster, routingRole attribute
        # for WiFi devices, this cluster doesn't exist.
        routing_role = node.attributes.get(ROUTING_ROLE_ATTRIBUTE_PATH)
        if routing_role is None:
            interval_ceiling = NODE_SUBSCRIPTION_CEILING_WIFI
        elif (
            routing_role
            == Clusters.ThreadNetworkDiagnostics.Enums.RoutingRoleEnum.kSleepyEndDevice
        ):
            interval_ceiling = NODE_SUBSCRIPTION_CEILING_BATTERY_POWERED
        else:
            interval_ceiling = NODE_SUBSCRIPTION_CEILING_THREAD
        self._last_subscription_attempt[node_id] = 0
        # set-up the actual subscription
        sub: Attribute.SubscriptionTransaction = (
            await self._chip_device_controller.read_attribute(
                node_id,
                [()],
                events=[("*", 1)],
                return_cluster_objects=False,
                report_interval=(interval_floor, interval_ceiling),
                auto_resubscribe=True,
            )
        )

        # Make sure to clear default handler which prints to stdout
        sub.SetAttributeUpdateCallback(None)
        sub.SetRawAttributeUpdateCallback(attribute_updated_callback)
        sub.SetEventUpdateCallback(event_callback)
        sub.SetErrorCallback(error_callback)
        sub.SetResubscriptionAttemptedCallback(resubscription_attempted)
        sub.SetResubscriptionSucceededCallback(resubscription_succeeded)

        node.available = True
        # update attributes with current state from read request
        tlv_attributes = sub.GetTLVAttributes()
        node.attributes.update(parse_attributes_from_read_result(tlv_attributes))

        report_interval_floor, report_interval_ceiling = (
            sub.GetReportingIntervalsSeconds()
        )
        node_logger.info(
            "Subscription succeeded with report interval [%d, %d]",
            report_interval_floor,
            report_interval_ceiling,
        )

        self._node_last_seen[node_id] = time.time()
        self.server.signal_event(EventType.NODE_UPDATED, node)

    def _get_next_node_id(self) -> int:
        """Return next node_id."""
        next_node_id = cast(int, self.server.storage.get(DATA_KEY_LAST_NODE_ID, 0)) + 1
        self.server.storage.set(DATA_KEY_LAST_NODE_ID, next_node_id, force=True)
        return next_node_id

    async def _setup_node(self, node_id: int) -> None:
        """Handle set-up of subscriptions and interview (if needed) for known/discovered node."""
        if node_id not in self._nodes:
            raise NodeNotExists(f"Node {node_id} does not exist.")
        if node_id in self._nodes_in_setup:
            # prevent duplicate setup actions
            return
        self._nodes_in_setup.add(node_id)
        node_logger = LOGGER.getChild(f"node_{node_id}")
        node_data = self._nodes[node_id]
        log_timers: dict[int, asyncio.TimerHandle] = {}

        async def log_node_long_setup(time_start: float) -> None:
            """Temporary measure to track a locked-up SDK issue in some (special) circumstances."""
            time_mins = int((time.time() - time_start) / 60)
            if TYPE_CHECKING:
                assert self.server.loop
            # get productlabel or modelname from raw attributes
            node_model = node_data.attributes.get(
                "0/40/14", node_data.attributes.get("0/40/3", "")
            )
            node_name = f"Node {node_id} ({node_model})"
            # get current IP the sdk is using to communicate with the device
            if sdk_ip_info := await self._chip_device_controller.get_address_and_port(
                node_id
            ):
                ip_address = sdk_ip_info[0]
            else:
                ip_address = "unknown"

            node_logger.error(
                f"\n\nATTENTION: {node_name} did not complete setup in {time_mins} minutes.\n"  # noqa: G004
                "This is an indication of a (connectivity) issue with this device. \n"
                f"IP-address in use for this device: {ip_address}\n"
                "Try powercycling this device and/or relocate it closer to a Border Router or \n"
                "WiFi Access Point. If this issue persists, please create an issue report on \n"
                "the Matter channel of the Home Assistant Discord server or on Github:\n"
                "https://github.com/home-assistant/core/issues/new?assignees=&labels="
                "integration%3A%20matter&projects=&template=bug_report.yml\n",
            )
            # reschedule itself
            log_timers[node_id] = self.server.loop.call_later(
                15 * 60, lambda: asyncio.create_task(log_node_long_setup(time_start))
            )

        async with self._node_setup_throttle:
            time_start = time.time()
            # we want to track nodes that take too long so we log it when we detect that
            if TYPE_CHECKING:
                assert self.server.loop
            log_timers[node_id] = self.server.loop.call_later(
                15 * 60, lambda: asyncio.create_task(log_node_long_setup(time_start))
            )
            try:
                node_logger.info("Setting-up node...")

                # try to resolve the node using the sdk first before do anything else
                try:
                    await self._chip_device_controller.find_or_establish_case_session(
                        node_id=node_id
                    )
                except NodeNotResolving as err:
                    node_logger.warning(
                        "Setup for node failed: %s",
                        str(err) or err.__class__.__name__,
                        # log full stack trace if verbose logging is enabled
                        exc_info=err
                        if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL)
                        else None,
                    )
                    # NOTE: the node will be picked up by mdns discovery automatically
                    # when it comes available again.
                    return

                # (re)interview node (only) if needed
                if (
                    # re-interview if we dont have any node attributes (empty node)
                    not node_data.attributes
                    # re-interview if the data model schema has changed
                    or node_data.interview_version != DATA_MODEL_SCHEMA_VERSION
                ):
                    try:
                        await self.interview_node(node_id)
                    except NodeInterviewFailed as err:
                        node_logger.warning(
                            "Setup for node failed: %s",
                            str(err) or err.__class__.__name__,
                            # log full stack trace if verbose logging is enabled
                            exc_info=err
                            if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL)
                            else None,
                        )
                        # NOTE: the node will be picked up by mdns discovery automatically
                        # when it comes available again.
                        return

                # setup subscriptions for the node
                try:
                    await self._subscribe_node(node_id)
                except ChipStackError as err:
                    node_logger.warning(
                        "Unable to subscribe to Node: %s",
                        str(err) or err.__class__.__name__,
                        # log full stack trace if verbose logging is enabled
                        exc_info=err
                        if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL)
                        else None,
                    )
                    # NOTE: the node will be picked up by mdns discovery automatically
                    # when it becomes available again.
                    return

                # check if this node has any custom clusters that need to be polled
                if polled_attributes := check_polled_attributes(node_data):
                    self._polled_attributes[node_id] = polled_attributes
                    self._schedule_custom_attributes_poller()

            finally:
                log_timers[node_id].cancel()
                self._nodes_in_setup.discard(node_id)

    def _handle_endpoints_removed(self, node_id: int, endpoints: Iterable[int]) -> None:
        """Handle callback for when bridge endpoint(s) get deleted."""
        node = self._nodes[node_id]
        for endpoint_id in endpoints:
            node.attributes = {
                key: value
                for key, value in node.attributes.items()
                if not key.startswith(f"{endpoint_id}/")
            }
            self.server.signal_event(
                EventType.ENDPOINT_REMOVED,
                {"node_id": node_id, "endpoint_id": endpoint_id},
            )
        # schedule save to persistent storage
        self._write_node_state(node_id)

    async def _handle_endpoints_added(
        self, node_id: int, endpoints: Iterable[int]
    ) -> None:
        """Handle callback for when bridge endpoint(s) get added."""
        # we simply do a full interview of the node
        await self.interview_node(node_id)
        # signal event to consumers
        for endpoint_id in endpoints:
            self.server.signal_event(
                EventType.ENDPOINT_ADDED,
                {"node_id": node_id, "endpoint_id": endpoint_id},
            )

    def _on_mdns_service_state_change(
        self,
        zeroconf: Zeroconf,  # pylint: disable=unused-argument
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # mdns events may arrive in bursts of (duplicate) messages
        # so we debounce this with a timer handle.
        if state_change == ServiceStateChange.Removed:
            # if we have an existing timer for this name, cancel it.
            if cancel := self._mdns_event_timer.pop(name, None):
                cancel.cancel()
            if service_type == MDNS_TYPE_OPERATIONAL_NODE:
                # we're not interested in operational node removals,
                # this is already handled by the subscription logic
                return

        if name in self._mdns_event_timer:
            # We already have a timer to resolve this service, so ignore this callback.
            return

        if TYPE_CHECKING:
            assert self.server.loop

        if service_type == MDNS_TYPE_COMMISSIONABLE_NODE:
            # process the event with a debounce timer
            self._mdns_event_timer[name] = self.server.loop.call_later(
                0.5, self._on_mdns_commissionable_node_state, name, state_change
            )
            return

        if service_type == MDNS_TYPE_OPERATIONAL_NODE:
            if self._fabric_id_hex is None or self._fabric_id_hex not in name.lower():
                # filter out messages that are not for our fabric
                return
        # process the event with a debounce timer
        self._mdns_event_timer[name] = self.server.loop.call_later(
            0.5, self._on_mdns_operational_node_state, name, state_change
        )

    def _on_mdns_operational_node_state(
        self, name: str, state_change: ServiceStateChange
    ) -> None:
        """Handle a (operational) Matter node MDNS state change."""
        self._mdns_event_timer.pop(name, None)
        logger = LOGGER.getChild("mdns")
        # the mdns name is constructed as [fabricid]-[nodeid]._matter._tcp.local.
        # extract the node id from the name
        node_id = int(name.split("-")[1].split(".")[0], 16)

        if not (node := self._nodes.get(node_id)):
            return  # this should not happen, but guard just in case

        now = time.time()
        last_seen = self._node_last_seen.get(node_id, 0)
        self._node_last_seen[node_id] = now

        # we only treat UPDATE state changes as ADD if the node is marked as
        # unavailable to ensure we catch a node being operational
        if node.available and state_change == ServiceStateChange.Updated:
            return

        if node_id in self._nodes_in_setup:
            # prevent duplicate setup actions
            return

        if not self._chip_device_controller.node_has_subscription(node_id):
            logger.info("Node %s discovered on MDNS", node_id)
        elif (now - last_seen) > NODE_MDNS_BACKOFF:
            # node came back online after being offline for a while or restarted
            logger.info("Node %s re-discovered on MDNS", node_id)
        else:
            # ignore all other cases
            return

        # setup the node - this will (re) setup the subscriptions etc.
        asyncio.create_task(self._setup_node(node_id))

    def _on_mdns_commissionable_node_state(
        self, name: str, state_change: ServiceStateChange
    ) -> None:
        """Handle a (commissionable) Matter node MDNS state change."""
        self._mdns_event_timer.pop(name, None)
        logger = LOGGER.getChild("mdns")

        try:
            info = AsyncServiceInfo(MDNS_TYPE_COMMISSIONABLE_NODE, name)
        except BadTypeInNameException as ex:
            logger.debug("Ignoring record with bad type in name: %s: %s", name, ex)
            return

        async def handle_commissionable_node_added() -> None:
            if TYPE_CHECKING:
                assert self._aiozc is not None
            await info.async_request(self._aiozc.zeroconf, 3000)
            logger.debug("Discovered commissionable Matter node: %s", info)

        if state_change == ServiceStateChange.Added:
            asyncio.create_task(handle_commissionable_node_added())
        elif state_change == ServiceStateChange.Removed:
            logger.debug("Commissionable Matter node disappeared: %s", info)

    def _write_node_state(self, node_id: int, force: bool = False) -> None:
        """Schedule the write of the current node state to persistent storage."""
        if node_id not in self._nodes:
            return  # guard
        if node_id >= TEST_NODE_START:
            return  # test nodes are stored in memory only
        node = self._nodes[node_id]
        self.server.storage.set(
            DATA_KEY_NODES,
            value=node,
            subkey=str(node_id),
            force=force,
        )

    async def _node_offline(self, node_id: int) -> None:
        """Mark node as offline."""
        # shutdown existing subscriptions
        await self._chip_device_controller.shutdown_subscription(node_id)
        # mark node as unavailable (if it wasn't already)
        node = self._nodes[node_id]
        if not node.available:
            return  # nothing to do to
        node.available = False
        self.server.signal_event(EventType.NODE_UPDATED, node)
        LOGGER.info("Marked node %s as offline", node_id)

    async def _fallback_node_scanner(self) -> None:
        """Scan for operational nodes in the background that are missed by mdns."""
        # This code could/should be removed in the future and is added to have a fallback
        # to discover operational nodes that got somehow missed by zeroconf.
        # the issue in zeroconf is being investigated and in the meanwhile we have this fallback.
        for node_id, node in self._nodes.items():
            if node.available:
                continue
            now = time.time()
            last_seen = self._node_last_seen.get(node_id, 0)
            if now - last_seen < FALLBACK_NODE_SCANNER_INTERVAL:
                continue
            if await self.ping_node(node_id, attempts=3):
                LOGGER.info("Node %s discovered using fallback ping", node_id)
                self._node_last_seen[node_id] = now
                await self._setup_node(node_id)

        # reschedule self to run at next interval
        self._schedule_fallback_scanner()

    def _schedule_fallback_scanner(self) -> None:
        """Schedule running the fallback node scanner at X interval."""
        if existing := self._fallback_node_scanner_timer:
            existing.cancel()

        def run_fallback_node_scanner() -> None:
            self._fallback_node_scanner_timer = None
            if (existing := self._fallback_node_scanner_task) and not existing.done():
                existing.cancel()
            self._fallback_node_scanner_task = asyncio.create_task(
                self._fallback_node_scanner()
            )

        if TYPE_CHECKING:
            assert self.server.loop
        self._fallback_node_scanner_timer = self.server.loop.call_later(
            FALLBACK_NODE_SCANNER_INTERVAL, run_fallback_node_scanner
        )

    async def _custom_attributes_poller(self) -> None:
        """Poll custom clusters/attributes for changes."""
        for node_id in tuple(self._polled_attributes):
            node = self._nodes[node_id]
            if not node.available:
                continue
            attribute_paths = list(self._polled_attributes[node_id])
            try:
                # try to read the attribute(s) - this will fire an event if the value changed
                await self.read_attribute(
                    node_id, attribute_paths, fabric_filtered=False
                )
            except (ChipStackError, NodeNotReady) as err:
                LOGGER.warning(
                    "Polling custom attribute(s) %s for node %s failed: %s",
                    ",".join(attribute_paths),
                    node_id,
                    str(err) or err.__class__.__name__,
                    # log full stack trace if verbose logging is enabled
                    exc_info=err if LOGGER.isEnabledFor(VERBOSE_LOG_LEVEL) else None,
                )
            # polling attributes is heavy on network traffic, so we throttle it a bit
            await asyncio.sleep(2)
        # reschedule self to run at next interval
        self._schedule_custom_attributes_poller()

    def _schedule_custom_attributes_poller(self) -> None:
        """Schedule running the custom clusters/attributes poller at X interval."""
        if existing := self._custom_attribute_poller_timer:
            existing.cancel()

        def run_custom_attributes_poller() -> None:
            self._custom_attribute_poller_timer = None
            if (existing := self._custom_attribute_poller_task) and not existing.done():
                existing.cancel()
            self._custom_attribute_poller_task = asyncio.create_task(
                self._custom_attributes_poller()
            )

        # no need to schedule the poll if we have no (more) custom attributes to poll
        if not self._polled_attributes:
            return

        if TYPE_CHECKING:
            assert self.server.loop
        self._custom_attribute_poller_timer = self.server.loop.call_later(
            CUSTOM_ATTRIBUTES_POLLER_INTERVAL, run_custom_attributes_poller
        )
