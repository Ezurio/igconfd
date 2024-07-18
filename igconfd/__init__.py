from syslog import syslog, openlog
from dbus.mainloop.glib import DBusGMainLoop
import systemd
import systemd.daemon

from . import customsvc
from . import configsvc

from gi.repository import GLib as glib

DEVICE_IG60 = "Sentrius IG60"
PROC_DEVICE_TREE_MODEL = "/proc/device-tree/model"
CONFIG_FILE = "/etc/ig60config"

def main():
    openlog("IG.ConfService")
    syslog("Starting main loop.")

    DBusGMainLoop(set_as_default=True)

    mainloop = glib.MainLoop()

    try:
        with open(PROC_DEVICE_TREE_MODEL, "r") as f:
            model = f.read().rstrip("\x00")
    except IOError as e:
        syslog("failed to read %s: %s" % (PROC_DEVICE_TREE_MODEL, e))
        return 1

    config = None
    try:
        with open(CONFIG_FILE, "r") as f:
            config = f.readline().rstrip("\n")
    except IOError as e:
        syslog("failed to read %s: %s" % (CONFIG_FILE, e))

    if config is not None:
        manager = customsvc.CustomService(config)
    else:
        manager = configsvc.ConfigurationService(model)

    manager.start()

    # Startup is complete, notify systemd
    systemd.daemon.notify("READY=1")
    mainloop.run()
