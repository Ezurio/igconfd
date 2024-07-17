"""
The ConfigurationService will initialize the Gatt application that does the BLE
work.
"""

import dbus, dbus.service, dbus.exceptions
import subprocess
from syslog import syslog

from .app import Application
from .messagemngr import MessageManager
from .netstat import NetStat
from .ltestat import LTEStat

from gi.repository import GObject as gobject


class ConfigurationService(Application):
    def __init__(self, device):
        bus = dbus.SystemBus()
        msg_manager = MessageManager(self.stop)
        wlan_mac_addr = msg_manager.net_manager.get_wlan_hw_address()
        device_name = "{} ({})".format(device, wlan_mac_addr[-8:])

        super().__init__(bus, device_name, msg_manager)

        self.msg_manager.start(self.vsp_svc.tx)
        self.init_ble_service()
        self.net_stat = NetStat(self.ConnectionStatsChanged)
        self.lte_stat = LTEStat(self.ConnectionStatsChanged)

    def start(self):
        syslog("Enabling BLE service.")
        self.register_le_services()
        subprocess.call(["bluetoothctl", "mgmt.power", "on"])

    def disable_ble_service(self):
        syslog("Disabling BLE service.")
        self.disconnect_devices()
        self.deregister_gatt_services()
        subprocess.call(["bluetoothctl", "mgmt.power", "off"])
        return False

    def stop(self):
        # Unregister LE Advertisement
        self.deregister_le_services()
        # Stop GATT service after a delay to allow last status message to be sent
        gobject.timeout_add(2000, self.disable_ble_service)
        # Stop message timeout callback
        self.msg_manager.set_msg_timeout(None, None)

