# prepperpi-ap

Wi-Fi access point service. Brings up `wlan0` as an AP with SSID `PrepperPi-<mac4>` on `10.42.0.1/24`, hands out DHCP leases via dnsmasq, and resolves all DNS queries to the AP (captive-portal trick).
