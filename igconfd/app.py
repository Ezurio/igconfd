"""BLE Device Manager functionality for the BLE configuration service
"""

import dbus, dbus.service, dbus.exceptions
import json
import os, os.path
import time
import subprocess
from syslog import syslog
from btsocket import btmgmt_sync

from . import leadvert
from . import vspsvc

from gi.repository import GObject as gobject

DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
ADAPTER_IFACE = "org.bluez.Adapter1"
BLUEZ_DEVICE_IFACE = "org.bluez.Device1"
LE_ADVERT_MGR_IFACE = "org.bluez.LEAdvertisingManager1"

BLUETOOTH_DEBUG_FS_BASE = "/sys/kernel/debug/bluetooth/hci0"

LE_CONN_MIN_INTERVAL = 12  # 15 ms
LE_CONN_MAX_INTERVAL = 24  # 30 ms
LE_CONN_LATENCY = 0
LE_SUPERVISION_TIMEOUT = 500  # 5000 ms
LE_ADV_MIN_INTERVAL = 200  # 125 ms
LE_ADV_MAX_INTERVAL = 800  # 500 ms

IGCONFD_SVC = "com.lairdtech.security.ConfigService"
IGCONFD_OBJ = "/com/lairdtech/security/ConfigService"

BT_MGMT_INSTANCE = 0
BT_MGMT_OFF = 0
BT_MGMT_ON = 1
BT_MGMT_IOCAP_NO_INPUT_NO_OUTPUT = 3

class Application(dbus.service.Object):
    """
    org.bluez.GattApplication1 interface implementation
    """

    def __init__(self, bus, device_name, msg_manager):
        self.path = IGCONFD_OBJ
        self.services = []

        self.bus = bus
        self.device_name = device_name
        self.msg_manager = msg_manager

        name = dbus.service.BusName(IGCONFD_SVC, bus=bus)
        super().__init__(name, self.path)

        # Start the VSP service
        self.vsp_svc = vspsvc.VirtualSerialPortService(bus, 0, self.rx_cb, self.disc_cb)
        self.add_service(self.vsp_svc)

        # Get the various Bluez Interfaces
        self.adapter = self.find_obj_by_iface(GATT_MANAGER_IFACE)
        self.gatt_manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, self.adapter), GATT_MANAGER_IFACE
        )
        self.advert_manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, self.adapter), LE_ADVERT_MGR_IFACE
        )
        self.le_adv_data = leadvert.LEAdvertData(
            bus, 0, [vspsvc.UUID_VSP_SVC], self.device_name
        )

        self.rx_timeout_id = None
        self.rx_message = None

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.signal("com.lairdtech.security.ConfigInterface", signature="i")
    def LTEStatusChanged(self, status):
        """
        Provides the status of the LTE connection
        """
        syslog("configsvc: lte status changed: %d" % status)
        return status

    @dbus.service.signal("com.lairdtech.security.ConfigInterface", signature="s")
    def ConnectionStatsChanged(self, connection_stats):
        syslog("configsvc: connection stats changed")
        return connection_stats

    @dbus.service.method(
        "com.lairdtech.security.ConfigInterface", in_signature="s", out_signature="i"
    )
    def ConnectLTE(self, config):
        try:
            lte_config = json.loads(config)
        except Exception as e:
            syslog("Configuration failed, exception = %s" % str(e))
            return -1

        self.msg_manager.net_manager.req_connect_lte(lte_config, self.LTEStatusChanged)
        return 0

    @dbus.service.method(
        "com.lairdtech.security.ConfigInterface", in_signature="s", out_signature="i"
    )
    def SetWifiConfigurations(self, config):
        try:
            wifi_configs = json.loads(config)
        except Exception as e:
            syslog("Configuration failed, exception = %s" % str(e))
            return -1

        self.msg_manager.net_manager.req_update_aps(wifi_configs)
        return 0

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        response = {}

        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrcs = service.get_characteristics()
            for chrc in chrcs:
                response[chrc.get_path()] = chrc.get_properties()
                descs = chrc.get_descriptors()
                for desc in descs:
                    response[desc.get_path()] = desc.get_properties()

        return response

    def rx_timeout(self):
        self.rx_message = None
        self.rx_timeout_id = None
        return False

    def rx_cb(self, message):
        self.rx_message = (self.rx_message or "") + message
        if self.rx_timeout_id:
            gobject.source_remove(self.rx_timeout_id)
            self.rx_timeout_id = None
        try:
            req_obj = json.loads(self.rx_message)
            self.vsp_svc.flush_tx()
            self.msg_manager.add_request(req_obj)
            self.rx_message = None
        except ValueError:
            # Couldn't parse JSON, set timeout for additional data
            self.rx_timeout_id = gobject.timeout_add(2000, self.rx_timeout)

    def disc_cb(self):
        syslog("Client disconnected.")
        self.msg_manager.client_disconnect()

    def find_objs_by_iface(self, iface):
        found_objs = []
        remote_om = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE
        )
        objects = remote_om.GetManagedObjects()
        for o, props in objects.items():
            if iface in props.keys():
                found_objs.append(o)
        return found_objs

    def find_obj_by_iface(self, iface):
        remote_om = dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE
        )
        objects = remote_om.GetManagedObjects()
        for o, props in objects.items():
            if iface in props.keys():
                return o
        return None

    def register_app_cb(self):
        syslog("GATT application registered")

    def register_app_error_cb(self, error):
        syslog("Failed to register application: " + str(error))

    def register_ad_cb(self):
        syslog("LE Advertisement registered.")

    def register_ad_error_cb(self, error):
        syslog("Failed to register LE advertisement: " + str(error))

    def disconnect_devices(self):
        device_objs = self.find_objs_by_iface(BLUEZ_DEVICE_IFACE)
        if device_objs:
            for d in device_objs:
                try:
                    syslog("Found device: {}".format(d))
                    dev = dbus.Interface(
                        self.bus.get_object(BLUEZ_SERVICE_NAME, d), BLUEZ_DEVICE_IFACE
                    )
                    dev_props = dbus.Interface(
                        self.bus.get_object(BLUEZ_SERVICE_NAME, d), DBUS_PROP_IFACE
                    )
                    if dev_props.Get(BLUEZ_DEVICE_IFACE, "Connected"):
                        syslog(
                            "Disconnecting device {}".format(
                                dev_props.Get(BLUEZ_DEVICE_IFACE, "Address")
                            )
                        )
                        dev.Disconnect()
                except dbus.exceptions.DBusException as e:
                    syslog("igconfd: disconnect_devices: %s" % e)
                    pass

    def write_debugfs_val(self, filename, value):
        syslog("Writing {} to {}".format(value, filename))
        file_path = os.path.join(BLUETOOTH_DEBUG_FS_BASE, filename)
        try:
            with open(file_path, "w") as f:
                f.write(str(value))
                f.close()
        except IOError as e:
            syslog("failed to write value {} to path {}".format(value, file_path))

    def init_ble_service(self):
        syslog("Configuring BLE advertisement settings.")
        # Use management API to configure BT
        btmgmt_sync.send("SetPowered", BT_MGMT_INSTANCE, BT_MGMT_OFF)
        btmgmt_sync.send("SetLowEnergy", BT_MGMT_INSTANCE, BT_MGMT_ON)
        btmgmt_sync.send("SetConnectable", BT_MGMT_INSTANCE, BT_MGMT_ON)
        btmgmt_sync.send("SetBREDR", BT_MGMT_INSTANCE, BT_MGMT_OFF)
        btmgmt_sync.send("SetIOCapability", BT_MGMT_INSTANCE, BT_MGMT_IOCAP_NO_INPUT_NO_OUTPUT, noresp=True) # No response data
        btmgmt_sync.send("SetBondable", BT_MGMT_INSTANCE, BT_MGMT_OFF)
        # Configure kernel BLE settings used in slave mode, that are only
        # available through debugfs
        self.write_debugfs_val("conn_max_interval", LE_CONN_MAX_INTERVAL)
        self.write_debugfs_val("conn_latency", LE_CONN_LATENCY)
        self.write_debugfs_val("supervision_timeout", LE_SUPERVISION_TIMEOUT)
        self.write_debugfs_val("adv_min_interval", LE_ADV_MIN_INTERVAL)
        # These settings are validated against previous writes, delay a bit
        time.sleep(1.0)
        self.write_debugfs_val("conn_min_interval", LE_CONN_MIN_INTERVAL)
        self.write_debugfs_val("adv_max_interval", LE_ADV_MAX_INTERVAL)

    def set_ble_power(self, powered):
        btmgmt_sync.send("SetPowered", BT_MGMT_INSTANCE, BT_MGMT_ON if powered else BT_MGMT_OFF)

    def register_le_services(self):
        syslog("Registering GATT application...")
        self.gatt_manager.RegisterApplication(
            self.get_path(),
            {},
            reply_handler=self.register_app_cb,
            error_handler=self.register_app_error_cb,
        )
        syslog("Registering LE Advertisement Data...")
        self.advert_manager.RegisterAdvertisement(
            self.le_adv_data.get_path(),
            {},
            reply_handler=self.register_ad_cb,
            error_handler=self.register_ad_error_cb,
        )

    def deregister_le_services(self):
        syslog("Unregistering LE Advertisement Data...")
        self.advert_manager.UnregisterAdvertisement(self.le_adv_data.get_path())

    def deregister_gatt_services(self):
        syslog("Unregistering GATT application...")
        self.gatt_manager.UnregisterApplication(self.get_path())
