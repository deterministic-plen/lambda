# GUI (`lib/GUI.lambda`)

Lambda GUI programs are ordinary Lambda values, not a windowing API embedded
in the language:

```lambda
type GUI = (width: Int, height: Int, pixels: ArrayB)
```

`pixels` holds `width * height * 3` bytes in **B, G, R** order (the order Win32
`StretchDIBits` expects).  A program that returns a `GUI` value opens a native
window on Windows.  Elsewhere, or with `LAMBDA_GUI_HEADLESS=1`, the runtime
writes a binary PPM image to `LAMBDA_GUI_OUT` (default `gui.ppm`) and prints the
path to stderr.  The PPM path makes GUI code testable in CI without a display.

## Helpers

All helpers consume a `GUI` and return the updated `GUI`, so scenes are linear:

| helper | meaning |
| --- | --- |
| `(canvas w h)` | black framebuffer |
| `(gui-plot g x y color)` | one pixel; `color` is `0xRRGGBB` as an Int |
| `(Rectangle g x y w h color)` | filled rectangle |
| `(circle g cx cy r color)` | filled circle |
| `(button g x y w h label)` | beveled button rectangle |

Example (`examples/plotter.lambda` draws a sine plot this way):

```lambda
include "lib/GUI.lambda"

g0 → (canvas 320 200)
g1 → (Rectangle g0 10 10 120 40 16711680)   ; red
g2 → (circle g1 220 80 40 65280)            ; green
g3 → (button g2 10 120 120 40 "OK")
g3
```

`Rectangle`, `circle` and `button` are themselves written in Lambda (no Python
plotting code is involved).  The label argument is accepted for API stability;
this framebuffer layer draws widget chrome and leaves glyph rasterisation to a
future native text pass.

## Native window

On Windows, `lambda_gui_show` (in `runtime_ext.py`) registers a window class,
draws the B,G,R buffer with GDI, and runs a normal message loop.  Close the
window or press Escape to exit.  The C ABI is one function:

```c
void lambda_gui_show(Value *gui);
```

and the GUI object is the data-backed product's normal boxed cons layout, so
no new value representation was needed.

## Pi

Pi can invoke a Lambda plotter as a compiled program (`python lambda.py
examples/plotter.lambda -o plotter`).  Passing a `GUI` value back through the
Pi worker protocol (for `pi view` or a native window) is not implemented yet;
Pi's existing CSV/LMB1/HTML artifacts remain the plotting output path.
