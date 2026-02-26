# Linux Setup Guide

This guide covers the system configuration required to run fichero-printer on Linux systems, particularly those with Intel Bluetooth adapters.

## Prerequisites

- Ubuntu 22.04+ or similar Linux distribution
- Python 3.10+
- BlueZ 5.60+ (the Linux Bluetooth stack)
- A Bluetooth adapter that supports BLE (Bluetooth Low Energy)

## Known Issue: BR/EDR Connection Error

### Symptom

When running `uv run fichero info`, you may encounter:

```
bleak.exc.BleakDBusError: [org.bluez.Error.Failed] br-connection-not-supported
```

This happens because BlueZ attempts a BR/EDR (Classic Bluetooth) connection instead of BLE, even though the printer only supports BLE for data transfer.

### Cause

The Fichero/D11s printer is a dual-mode Bluetooth device that advertises both Classic Bluetooth and BLE capabilities. Some Bluetooth adapters (particularly Intel adapters) and BlueZ configurations default to Classic Bluetooth connections, which fail for this device.

### Solution: Disable BR/EDR Mode

Create a systemd service that disables BR/EDR (Classic Bluetooth) at boot, forcing all connections to use BLE.

#### Step 1: Create the service file

```bash
sudo nano /etc/systemd/system/ble-only.service
```

Add the following content:

```ini
[Unit]
Description=Disable BR/EDR Bluetooth (BLE only mode)
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=oneshot
ExecStart=/usr/bin/btmgmt power off
ExecStart=/usr/bin/btmgmt bredr off
ExecStart=/usr/bin/btmgmt power on
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

#### Step 2: Enable the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable ble-only.service
sudo systemctl start ble-only.service
```

#### Step 3: Verify

```bash
sudo btmgmt info | grep "current settings"
```

The output should NOT include "br/edr". Example:
```
current settings: powered le secure-conn
```

### Warning

Disabling BR/EDR affects ALL Bluetooth devices on this system:
- Bluetooth audio devices (headphones, speakers) may stop working
- Bluetooth keyboards/mice that use Classic Bluetooth may stop working
- Only BLE devices will be able to connect

If you need Classic Bluetooth for other devices, consider using a dedicated USB BLE dongle for the printer.

## BlueZ Experimental Mode (Optional)

Some BLE features require BlueZ experimental mode. To enable it:

```bash
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo bash -c 'cat > /etc/systemd/system/bluetooth.service.d/override.conf << EOF
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --experimental
EOF'
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
```

## Troubleshooting

### Device not found during scan

1. Ensure the printer is turned on (green LED)
2. Check if Bluetooth is enabled: `bluetoothctl show`
3. Try resetting the Bluetooth adapter: `sudo hciconfig hci0 reset`

### Permission denied errors

Add your user to the `bluetooth` group:

```bash
sudo usermod -a -G bluetooth $USER
```

Then log out and back in.

### Debugging BLE connections

Monitor Bluetooth HCI traffic:

```bash
sudo btmon
```

In another terminal, run the fichero command to see the raw Bluetooth communication.

### Check BlueZ version

```bash
bluetoothctl --version
```

Ensure you have BlueZ 5.60 or newer for best BLE support.

## Tested Configurations

| Distribution | BlueZ Version | Bluetooth Adapter | Status |
|--------------|---------------|-------------------|--------|
| Ubuntu 24.04 | 5.72 | Intel AX201 | Works with BR/EDR disabled |

## Web GUI Setup (Optional)

To host the web GUI on an Apache server:

```bash
sudo mkdir -p /var/www/html/fichero
sudo cp web/index.html /var/www/html/fichero/
sudo chown -R www-data:www-data /var/www/html/fichero
```

Access via: `http://<server-ip>/fichero/`

Note: The Web Bluetooth API requires HTTPS or localhost. For remote access, configure SSL/TLS on your web server.
