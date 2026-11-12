#!/bin/bash
# Hand the SPI/GPIO pins back to a314d after fomu-flash (from tf4060/boards/tf4060r5/setgpio.sh).
pinctrl set 7 op dh   # A314 select idle (a314d drives it for the SPI bus handshake, tf4060v2 #123)
pinctrl set 8 ip
pinctrl set 9  a0
pinctrl set 10 a0
pinctrl set 11 a0
pinctrl set 18 ip
pinctrl set 25 op
