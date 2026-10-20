# RTU Point Simulator

Windows desktop tool for sending simulated values to the RTU's IEC-104 monitor points. The
catalog mirrors the firmware's current `point_catalog.c`: 122 monitor points (55 SINGLE,
1 DOUBLE, and 66 FLOAT). Command IOAs are intentionally not included because command
execution is not configured in the firmware.

## Run

Install Python 3 with Tk support, then run from this directory:

```powershell
python app.py
```

The default destination is `192.168.0.200:3001`. Change the address and port in the app if needed.

## Add a point

The UI is generated from `points.json`, with a filter and scrollable list for larger catalogs.
When the firmware catalog changes, keep this file's IOA, name, and type entries aligned with
`Core/Src/iec104/point_catalog.c`. Supported types: `SINGLE`, `DOUBLE`, `STEP`, `NORMALIZED`,
`SCALED`, and `FLOAT`. Current simulator limits match the RTU receiver: SINGLE 0–1, DOUBLE
0–3, and FLOAT -10000 to 10000.

The catalog controls what the UI displays and validates. The RTU must independently whitelist the IOA and validate the value before updating its database.

## TCP line protocol

The client sends one ASCII line for each update:

```text
SET <IOA> <TYPE> <VALUE>\n
```

Expected responses are `OK <IOA>` or `ERR [<IOA>] <reason>`, each terminated by a newline. The client accumulates partial TCP response lines and handles multiple lines arriving together.

The simulator uses a separate TCP connection; IEC-104 communication remains independent. The firmware needs a receiver implementing this protocol before the app can update the board.
