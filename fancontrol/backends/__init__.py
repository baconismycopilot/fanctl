"""Hardware backends.

No single tool covers this hardware: liquidctl handles fans, pump, curves, LCD
and hub RGB, but cannot touch the Kraken's pump light ring, which only OpenRGB
can drive. The CLI presents one surface over both.
"""
