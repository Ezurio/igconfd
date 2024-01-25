import dbus, dbus.service, dbus.exceptions
import json
import urllib, urllib.request, urllib.error

from .netmngr import NetManager
from .provmngr import ProvManager
from .devmngr import DeviceManager

from syslog import syslog

from gi.repository import GObject as gobject

# JSON Message Strings
MSG_VERSION = "version"
MSG_ID = "id"
MSG_TYPE = "type"
MSG_STATUS = "status"
MSG_DATA = "data"

MSG_VERSION_VAL = 4

MSG_ID_VERSION = "version"
MSG_ID_GET_DEVICE_ID = "getDeviceId"
MSG_ID_GET_APS = "getAccessPoints"
MSG_ID_CONNECT_AP = "connectAP"
MSG_ID_UPDATE_APS = "updateAPS"
MSG_ID_GET_CURRENT_APS = "getAPS"
MSG_ID_CONNECT_LTE = "connectLTE"
MSG_ID_PROVISION_URL = "provisionURL"
MSG_ID_PROVISION_EDGE = "provisionEdge"
MSG_ID_GET_DEVICE_CAPS = "getDeviceCaps"
MSG_ID_GET_STORAGE_INFO = "getStorageInfo"
MSG_ID_EXT_STORAGE_SWAP = "extStorageSwap"
MSG_ID_GET_LTE_INFO = "getLTEInfo"
MSG_ID_GET_LTE_STATUS = "getLTEStatus"
MSG_ID_CONN_CHECK = "connCheck"
MSG_ID_UPDATE_CONFIG = "updateConfig"
MSG_ID_CHECK_UPDATE = "checkUpdate"

MSG_STATUS_INTERMEDIATE = 1
MSG_STATUS_SUCCESS = 0
MSG_STATUS_ERR_INVALID = -1
MSG_STATUS_ERR_TIMEOUT = -2
MSG_STATUS_ERR_AUTH = -3
MSG_STATUS_ERR_NOTFOUND = -4
MSG_STATUS_ERR_NOCONN = -5
MSG_STATUS_ERR_DEVICE = -6
MSG_STATUS_API_DISABLED = -7
MSG_STATUS_ERR_NOSIM = -8
MSG_STATUS_ERR_BAD_CONFIG = -9
MSG_STATUS_ERR_UNKNOWN = -10

EXT_STORAGE_STATUS_FULL = -1
EXT_STORAGE_STATUS_FAILED = -2
EXT_STORAGE_STATUS_STOP_FAILED = -3
EXT_STORAGE_STATUS_READY = 0
EXT_STORAGE_STATUS_NOTPRESENT = 1
EXT_STORAGE_STATUS_UNFORMATTED = 2
EXT_STORAGE_STATUS_FORMATTING = 3
EXT_STORAGE_STATUS_STOPPING = 4
EXT_STORAGE_STATUS_STOPPED = 5

STORAGE_EJECTING = "ejecting"
STORAGE_STOPPED = "stopped"
STORAGE_INSERTING = "inserting"
STORAGE_FORMATTING = "formatting"

PROVISION_INTERMEDIATE_TIMEOUT = 2
PROVISION_TIMER_MS = 500

DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
EXT_STORAGE_STATUS_PROP = "ExtStorageStatus"

DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
BLUEZ_SERVICE_NAME = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"

UPDATE_SVC = "com.lairdtech.security.UpdateService"
UPDATE_PATH = "/com/lairdtech/security/UpdateService"
UPDATE_IFACE = "com.lairdtech.security.UpdateInterface"
UPDATE_PUBLIC_IFACE = "com.lairdtech.security.public.UpdateInterface"

STORAGE_SWAP_TIMER_MS = 2000


def convert_dict_keys_values_to_string(data):
    """Convert dict's keys & values from 'bytes' to 'string'"""
    if isinstance(data, bytes):
        return data.decode("utf-8")
    if isinstance(data, dict):
        return dict(map(convert_dict_keys_values_to_string, data.items()))
    if isinstance(data, tuple):
        return tuple(map(convert_dict_keys_values_to_string, data))
    if isinstance(data, list):
        return list(map(convert_dict_keys_values_to_string, data))
    if isinstance(data, set):
        return set(map(convert_dict_keys_values_to_string, data))
    return data


class MessageManager:
    def __init__(self, shutdown_cb):
        self.prov_manager = ProvManager(self.send_prov_response)
        self.dev_manager = DeviceManager(self.send_dev_response)
        self.net_manager = NetManager(self.send_net_response)

        self.shutdown_cb = shutdown_cb
        self.cur_dev_storageinfo_req_obj = None
        self.cur_dev_storageswap_req_obj = None
        self.msg_timeout_id = None
        self.msg_timeout_cb = None
        self.msg_timeout_delay = None

        self.bus = dbus.SystemBus()
        self.bluez_dev_obj = self.bus.get_object(
            BLUEZ_SERVICE_NAME, self.find_obj_by_iface(ADAPTER_IFACE)
        )
        self.bluez_dev_props = dbus.Interface(self.bluez_dev_obj, DBUS_PROP_IFACE)

    def start(self, tx_msg):
        self.tx_msg = tx_msg

    def add_request(self, req_obj):
        """Schedule request handler to run on main loop"""
        # Cancel any current AP scan
        self.net_manager.stop_scanning()
        gobject.timeout_add(0, self.handle_command, req_obj)

    def client_disconnect(self):
        # Reset message state on client disconnect
        syslog("BLE client disconnected, resetting state.")
        self.net_manager.stop_scanning()

    def reset_msg_timeout(self):
        if self.msg_timeout_id is not None:
            gobject.source_remove(self.msg_timeout_id)
            self.msg_timeout_id = None
        if self.msg_timeout_cb is not None and self.msg_timeout_delay is not None:
            self.msg_timeout_id = gobject.timeout_add(
                self.msg_timeout_delay, self.msg_timeout_cb
            )

    def set_msg_timeout(self, msg_timeout_delay, msg_timeout_cb):
        self.msg_timeout_delay = msg_timeout_delay
        self.msg_timeout_cb = msg_timeout_cb
        self.reset_msg_timeout()

    def send_response(self, req_obj, status, data=None, tx_complete=None):
        """Send a response message based on the request, with optional data"""
        self.reset_msg_timeout()
        try:
            resp_obj = {
                MSG_VERSION: MSG_VERSION_VAL,
                MSG_ID: req_obj[MSG_ID],
                MSG_TYPE: req_obj[MSG_TYPE],
                MSG_STATUS: status,
            }
            if data:
                resp_obj[MSG_DATA] = data
            syslog(
                "Sending {} response ({})".format(
                    resp_obj[MSG_TYPE], resp_obj[MSG_STATUS]
                )
            )
            self.tx_msg(json.dumps(resp_obj, separators=(",", ":")), tx_complete)
        except Exception as e:
            syslog("Failed to send response: '%s'" % str(e))

    def handle_command(self, req_obj):
        """Process a request object"""
        self.reset_msg_timeout()
        try:
            msg_type = req_obj[MSG_TYPE]
            syslog("Processing request: {}".format(msg_type))
            # Check version on all messages except the version check;
            # don't allow future versions (client must be backwards
            # compatible)
            if msg_type != MSG_ID_VERSION and req_obj[MSG_VERSION] > MSG_VERSION_VAL:
                self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
                return
            if msg_type == MSG_ID_VERSION:
                self.send_response(req_obj, MSG_STATUS_SUCCESS)
            elif msg_type == MSG_ID_GET_DEVICE_ID:
                self.req_get_device_id(req_obj)
            elif msg_type == MSG_ID_GET_DEVICE_CAPS:
                self.req_get_device_caps(req_obj)
            elif msg_type == MSG_ID_GET_APS:
                self.handle_net_manager_request(MSG_ID_GET_APS, req_obj)
            elif msg_type == MSG_ID_CONNECT_AP:
                self.handle_net_manager_request(MSG_ID_CONNECT_AP, req_obj)
            elif msg_type == MSG_ID_UPDATE_APS:
                self.handle_net_manager_request(MSG_ID_UPDATE_APS, req_obj)
            elif msg_type == MSG_ID_GET_CURRENT_APS:
                self.handle_net_manager_request(MSG_ID_GET_CURRENT_APS, req_obj)
            elif msg_type == MSG_ID_CONNECT_LTE:
                self.handle_net_manager_request(MSG_ID_CONNECT_LTE, req_obj)
            elif msg_type == MSG_ID_PROVISION_URL:
                self.handle_prov_manager_request(MSG_ID_PROVISION_URL, req_obj)
            elif msg_type == MSG_ID_PROVISION_EDGE:
                self.handle_prov_manager_request(MSG_ID_PROVISION_EDGE, req_obj)
            elif msg_type == MSG_ID_GET_STORAGE_INFO:
                self.handle_dev_manager_request(MSG_ID_GET_STORAGE_INFO, req_obj)
            elif msg_type == MSG_ID_EXT_STORAGE_SWAP:
                self.handle_dev_manager_request(MSG_ID_EXT_STORAGE_SWAP, req_obj)
            elif msg_type == MSG_ID_GET_LTE_INFO:
                self.handle_net_manager_request(MSG_ID_GET_LTE_INFO, req_obj)
            elif msg_type == MSG_ID_GET_LTE_STATUS:
                self.handle_net_manager_request(MSG_ID_GET_LTE_STATUS, req_obj)
            elif msg_type == MSG_ID_CONN_CHECK:
                self.req_conn_check(req_obj)
            elif msg_type == MSG_ID_UPDATE_CONFIG:
                self.req_update_config(req_obj)
            elif msg_type == MSG_ID_CHECK_UPDATE:
                self.req_check_update(req_obj)
            else:
                self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except KeyError:
            syslog("Invalid request message, ignoring.")
        except Exception as e:
            syslog("Unexpected failure: {}".format(e))
        # Exit timer
        return False

    def req_get_device_id(self, req_obj):
        """Handle Get Device ID request"""
        # Read version as last string in release file line
        version = ""
        with open("/etc/os-release", "r") as f:
            for line in f:
                if "VERSION_ID" in line:
                    version = line.rstrip().split("=")[-1]
                    break

        # Read Name from bluez dbus object
        name = str(self.bluez_dev_props.Get(ADAPTER_IFACE, "Name"))

        id_data = {
            "deviceId": self.net_manager.get_wlan_hw_address(),
            "eth0addr" : self.net_manager.get_eth0_hw_address(),
            "name": name,
            "devType": int(self.dev_manager.get_device_type()),
            "version": version,
        }
        self.send_response(req_obj, MSG_STATUS_SUCCESS, data=id_data)

    def req_get_device_caps(self, req_obj):
        """Handle Get Device Capabilities Request"""
        cap_data = {}

        cap_data["isProvisioned"] = self.prov_manager.is_provisioned()

        if self.prov_manager.api_enabled:
            cap_data.setdefault("deviceCaps", []).append("provisionURL")
            cap_data.setdefault("deviceCaps", []).extend(
                ["getStorageInfo", "extStorageSwap"]
            )

        if self.net_manager.is_modem_available():
            cap_data.setdefault("deviceCaps", []).append("connectLTE")

        self.send_response(req_obj, MSG_STATUS_SUCCESS, data=cap_data)

    def req_conn_check(self, req_obj):
        """Handle Connectivity Check Request"""
        try:
            url = req_obj["data"]["url"]
            check_timeout = float(req_obj["data"]["timeout"])
            syslog(
                "Performing connectivity check on {} with timeout {}".format(
                    url, check_timeout
                )
            )
            req = urllib.request.Request(url)
            r = urllib.request.urlopen(req, timeout=check_timeout)
            resp_data = {}
            resp_data["result"] = r.status
            if r.length is None:
                content = r.read()
                resp_data["len"] = len(content)
            else:
                resp_data["len"] = r.length
            r.close()
            self.send_response(req_obj, MSG_STATUS_SUCCESS, data=resp_data)
        except TimeoutError:
            self.send_response(req_obj, MSG_STATUS_ERR_TIMEOUT)
        except urllib.error.URLError:
            self.send_response(req_obj, MSG_STATUS_ERR_NOCONN)
        except KeyError:
            # Invalid request
            self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except ValueError:
            # Invalid request
            self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except:
            # Hmmm, something else went wrong
            self.send_response(req_obj, MSG_STATUS_ERR_UNKNOWN)

    #
    # Response callbacks for the various service managers
    #
    def send_net_response(self, status, data=None, tx_complete=None):
        if (
            status == NetManager.ACTIVATION_SUCCESS
            or status == NetManager.AP_SCANNING_SUCCESS
        ):
            self.send_response(self.cur_net_req_obj, MSG_STATUS_SUCCESS, data)
        elif status == NetManager.ACTIVATION_FAILED_AUTH:
            self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_AUTH)
        elif status == NetManager.ACTIVATION_FAILED_NETWORK:
            self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_NOCONN)
        elif status == NetManager.ACTIVATION_NO_SIM:
            self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_NOSIM)
        elif status == NetManager.ACTIVATION_NO_CONN:
            self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_NOCONN)
            self.net_manager.activation_cleanup()
        elif (
            status == NetManager.ACTIVATION_PENDING or status == NetManager.AP_SCANNING
        ):
            self.send_response(
                self.cur_net_req_obj, MSG_STATUS_INTERMEDIATE, data, tx_complete
            )

    def lte_autoconnect_status(self, status, data=None, tx_complete=None):
        if status == NetManager.ACTIVATION_PENDING or status == NetManager.AP_SCANNING:
            # LTE connect is still pending, send intermediate status
            resp_data = {"operation": "LTE autoconnect"}
            self.send_response(
                self.cur_prov_req_obj, MSG_STATUS_INTERMEDIATE, resp_data, tx_complete
            )
        else:
            # LTE connect has completed (success or failure); continue provisioning
            self.prov_manager.start_provisioning(
                convert_dict_keys_values_to_string(self.cur_prov_req_obj["data"])
            )

    def send_prov_response(self, status, data=None):
        if status == ProvManager.PROV_COMPLETE_SUCCESS:
            # Shutdown provisioning service and disabled the api
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_SUCCESS)

            # Only disable the API if Greengrass was just provisioned (leave it
            # enabled for Edge IQ)
            if self.prov_manager.get_greengrass_prov_state == True:
                self.prov_manager.disable_api()

            self.shutdown_cb()
        elif status == ProvManager.PROV_FAILED_AUTH:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_AUTH)
        elif status == ProvManager.PROV_FAILED_TIMEOUT:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_TIMEOUT)
        elif status == ProvManager.PROV_FAILED_CONNECT:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_NOCONN)
        elif status == ProvManager.PROV_FAILED_NOT_FOUND:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_NOTFOUND)
        elif status == ProvManager.PROV_FAILED_BAD_CONFIG:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_BAD_CONFIG)
        elif status == ProvManager.PROV_FAILED_INVALID:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_INVALID)
        elif status < 0:  # All other failures
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_ERR_UNKNOWN)
        else:
            self.send_response(self.cur_prov_req_obj, MSG_STATUS_INTERMEDIATE, data)

    def send_dev_response(self, status, data=None):
        self.send_response(self.cur_dev_storageswap_req_obj, status, data)

    #
    # Request handlers for the various service managers
    #
    def handle_net_manager_request(self, msg_type, req_obj):
        self.cur_net_req_obj = req_obj

        if self.prov_manager.api_enabled and self.prov_manager.is_provisioned():
            self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_INVALID)
            return

        if msg_type == MSG_ID_GET_APS:
            self.net_manager.req_get_access_points()
        elif msg_type == MSG_ID_CONNECT_AP and "data" in req_obj:
            self.net_manager.req_connect_ap(
                convert_dict_keys_values_to_string(self.cur_net_req_obj["data"])
            )
        elif msg_type == MSG_ID_CONNECT_LTE:
            if "data" in req_obj:
                params = convert_dict_keys_values_to_string(
                    self.cur_net_req_obj["data"]
                )
            else:
                params = {}
            self.net_manager.req_connect_lte(params)
        elif msg_type == MSG_ID_UPDATE_APS and "data" in self.cur_net_req_obj:
            ret = self.net_manager.req_update_aps(
                convert_dict_keys_values_to_string(self.cur_net_req_obj["data"])
            )
            if ret:
                self.send_net_response(NetManager.ACTIVATION_SUCCESS)
            else:
                self.send_net_response(NetManager.ACTIVATION_FAILED_NETWORK)
        elif msg_type == MSG_ID_GET_CURRENT_APS:
            aps = self.net_manager.req_get_aps()
            if aps is not None:
                self.send_net_response(NetManager.ACTIVATION_SUCCESS, data=aps)
            else:
                self.send_net_response(NetManager.ACTIVATION_FAILED_NETWORK)
        elif msg_type == MSG_ID_GET_LTE_INFO:
            lte_info = self.net_manager.req_get_lte_info()
            if lte_info is not None:
                self.send_response(self.cur_net_req_obj, MSG_STATUS_SUCCESS, lte_info)
            else:
                self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_INVALID)
        elif msg_type == MSG_ID_GET_LTE_STATUS:
            lte_status = self.net_manager.req_get_lte_status()
            if lte_status is not None:
                self.send_response(
                    self.cur_net_req_obj, MSG_STATUS_SUCCESS, data=lte_status
                )
            else:
                self.send_response(self.cur_net_req_obj, MSG_STATUS_ERR_INVALID)

    def handle_prov_manager_request(self, msg_type, req_obj):

        if self.net_manager.api_enabled:
            self.net_manager.stop_scanning()

        self.cur_prov_req_obj = req_obj
        if self.prov_manager.api_enabled:
            if msg_type == MSG_ID_PROVISION_URL and "data" in req_obj:
                # If the LTE modem is available and has not been
                # configured, AND this request has the legacy version (1),
                # configure the default LTE profile before performing
                # provisioning via URL; this enables use of the
                # LTE modem when using the legacy mobile application.
                if (
                    req_obj["version"] == 1
                    and self.net_manager.is_modem_available()
                    and not self.net_manager.is_lte_configured()
                ):
                    self.net_manager.req_connect_lte({}, self.lte_autoconnect_status)
                else:
                    self.prov_manager.start_provisioning(
                        convert_dict_keys_values_to_string(req_obj["data"])
                    )
            elif msg_type == MSG_ID_PROVISION_EDGE and "data" in req_obj:
                self.prov_manager.start_provisioning_edge(
                    convert_dict_keys_values_to_string(req_obj["data"])
                )
        else:
            self.send_prov_response(MSG_STATUS_ERR_INVALID, self.cur_prov_req_obj)

    def handle_dev_manager_request(self, msg_type, req_obj):
        if self.dev_manager.api_enabled:

            if msg_type == MSG_ID_GET_STORAGE_INFO:
                self.cur_dev_storageinfo_req_obj = req_obj
                storage_data = self.dev_manager.get_storage_data()
                self.send_response(
                    self.cur_dev_storageinfo_req_obj,
                    MSG_STATUS_SUCCESS,
                    data=storage_data,
                )
            elif msg_type == MSG_ID_EXT_STORAGE_SWAP:
                self.cur_dev_storageswap_req_obj = req_obj
                status, storage_data = self.dev_manager.do_storage_swap()
                self.send_response(
                    self.cur_dev_storageswap_req_obj, status, data=storage_data
                )
        else:
            self.send_response(req_obj, MSG_STATUS_ERR_INVALID)

    def find_obj_by_iface(self, iface):
        remote_om = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE
        )
        objects = remote_om.GetManagedObjects()
        for o, props in objects.items():
            if iface in props.keys():
                return o
        return None

    def req_update_config(self, req_obj):
        try:
            updatesvc = dbus.Interface(
                self.bus.get_object(UPDATE_SVC, UPDATE_PATH), UPDATE_IFACE
            )
            ret = int(updatesvc.SetConfiguration(json.dumps(req_obj["data"])))
            if ret == 0:
                syslog("Update request success.")
                self.send_response(req_obj, MSG_STATUS_SUCCESS)
            else:
                syslog("Update request failed.")
                self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except dbus.DBusException:
            syslog("Failed to connect to the Update service.")
            self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except (KeyError, ValueError, TypeError) as e:
            # Invalid request
            syslog("Invalid update request.")
            self.send_response(req_obj, MSG_STATUS_ERR_INVALID)
        except:
            # Hmmm, something else went wrong
            self.send_response(req_obj, MSG_STATUS_ERR_UNKNOWN)

    def req_check_update(self, req_obj):
        try:
            updatesvc = dbus.Interface(
                self.bus.get_object(UPDATE_SVC, UPDATE_PATH), UPDATE_PUBLIC_IFACE
            )
            ret = int(updatesvc.CheckUpdate(False))
            syslog("Update check status: {}".format(ret))
            self.send_response(req_obj, MSG_STATUS_SUCCESS, data={"updateStatus": ret})
        except:
            # Hmmm, something went wrong
            self.send_response(req_obj, MSG_STATUS_ERR_UNKNOWN)
