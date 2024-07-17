"""
The CustomService will extend the the ConfigurationService to implement
the features specific to the IG60.
"""

import dbus, dbus.service, dbus.exceptions
import subprocess
from syslog import syslog

from .configsvc import ConfigurationService

from gi.repository import GObject as gobject

# DBus paths for the IG services
DEVICE_SVC_NAME = "com.lairdtech.device.DeviceService"
DEVICE_SVC_PATH = "/com/lairdtech/device/DeviceService"
DEVICE_IFACE = "com.lairdtech.device.DeviceInterface"
PROV_SVC = "com.lairdtech.IG.ProvService"
PROV_IFACE = "com.lairdtech.IG.ProvInterface"
PROV_OBJ = "/com/lairdtech/IG/ProvService"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

PROV_COMPLETE_SUCCESS = 0
BUTTON_PRESS_SHORT = 0

BLE_STATE_ACTIVE = 1
BLE_STATE_INACTIVE = 0

BUTTON_PRESS_MSG_TIMEOUT_MS = 60000


class CustomService(ConfigurationService):
    def __init__(self, device):

        ConfigurationService.__init__(self, device)
        syslog("Starting Sentrius IG60 configuration service.")
        self.device_svc = dbus.Interface(
            self.bus.get_object(DEVICE_SVC_NAME, DEVICE_SVC_PATH), DEVICE_IFACE
        )
        self.device_svc.connect_to_signal("ConfigButtonPress", self.config_button_press)
        self.prov = dbus.Interface(self.bus.get_object(PROV_SVC, PROV_OBJ), PROV_IFACE)
        self.prov_props = dbus.Interface(
            self.bus.get_object(PROV_SVC, PROV_OBJ), DBUS_PROP_IFACE
        )
        self.prov.connect_to_signal("StateChanged", self.prov_state_changed)
        self.prov_state = self.prov_props.Get(PROV_IFACE, "Status")
        self.greengrass_prov_state = self.prov_props.Get(
            PROV_IFACE, "GreengrassProvisioned"
        )
        self.edge_iq_prov_state = self.prov_props.Get(PROV_IFACE, "EdgeIQProvisioned")
        # Request the device service to turn on the LTE modem (if present)
        self.device_svc.LTE_On()

    def enable_ble_service(self):
        syslog("Enabling BLE service.")
        self.register_le_services()
        self.device_svc.SetBLEState(BLE_STATE_ACTIVE)
        subprocess.call(["bluetoothctl", "mgmt.power", "on"])

    def start(self):
        if self.greengrass_prov_state or self.edge_iq_prov_state:
            syslog("Device is provisioned, skipping BLE service.")
        else:
            syslog("Device is not provisioned, starting BLE service...")
            self.enable_ble_service()

    def stop(self):
        # Unregister LE Advertisement
        self.deregister_le_services()
        # Turn off configuration LED
        self.device_svc.SetBLEState(BLE_STATE_INACTIVE)
        # Stop GATT service after a delay to allow last status message to be sent
        gobject.timeout_add(2000, self.disable_ble_service)
        # Stop message timeout callback
        self.msg_manager.set_msg_timeout(None, None)

    def prov_state_changed(self, state):
        self.prov_state = state

        # Update the provisioned state
        self.greengrass_prov_state = self.prov_props.Get(
            PROV_IFACE, "GreengrassProvisioned"
        )
        self.edge_iq_prov_state = self.prov_props.Get(PROV_IFACE, "EdgeIQProvisioned")
        syslog("Greengrass provisioned state = %s" % str(self.greengrass_prov_state))
        syslog("EdgeIQ provisioned state = %s" % str(self.edge_iq_prov_state))

    def config_button_press(self, press_type):
        # If button is pressed (short) and already provisioned, enable service
        if press_type == BUTTON_PRESS_SHORT and (
            self.greengrass_prov_state or self.edge_iq_prov_state
        ):
            syslog("Config button pressed, enabling BLE service.")
            self.enable_ble_service()
            # Set message timeout callback to disable service after inactivity
            self.msg_manager.set_msg_timeout(BUTTON_PRESS_MSG_TIMEOUT_MS, self.stop)
            # Make sure AP scan list gets updated
            self.msg_manager.net_manager.start_ap_scan()
