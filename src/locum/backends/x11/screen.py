"""Screen part of the X11 backend: virtual screen, RandR monitors and screenshots
(mss with an ``XGetImage`` fallback), as a mixin of :class:`X11Backend`.
"""

from __future__ import annotations

import os
import weakref
from typing import Any, List, Optional, Tuple

from ...errors import BackendError, NotSupported
from ...types import Monitor, Rect
from ..base import is_blank_image
from ._xcore import _close_quietly, _x_errors


class _MssHolder:
    """Thread-local owner of one mss grabber; its finalizer closes the grabber."""

    __slots__ = ("sct", "__weakref__")

    def __init__(self, sct: Any) -> None:
        self.sct = sct


class _ScreenMixin:
    """Screen geometry, monitors and screenshots of :class:`X11Backend`."""

    # =================================================================== screen
    @_x_errors
    def virtual_screen(self) -> Rect:
        self._check()
        with self._lock:
            w, h = self._screen_size()
        return Rect(0, 0, w, h)

    def _xft_scale(self) -> float:
        try:
            atom = self._d.get_atom("RESOURCE_MANAGER")
            prop = self._root.get_full_property(atom, self._xl.Xatom.STRING)
            if prop is None:
                return 1.0
            data = prop.value
            if isinstance(data, bytes):
                data = data.decode("latin-1", "replace")
            for line in str(data).splitlines():
                k, _, v = line.partition(":")
                if k.strip() == "Xft.dpi":
                    dpi = float(v.strip())
                    if dpi > 0:
                        return round(dpi / 96.0, 3)
        except Exception:
            pass
        return 1.0

    @_x_errors
    def monitors(self) -> List[Monitor]:
        """Monitors from RandR (primary first, then left-to-right, top-to-bottom)."""
        self._check()
        with self._lock:
            scale = self._xft_scale()
            raw: List[Tuple[Rect, bool, str]] = []
            if self._randr:
                raw = self._randr_monitors()
            if not raw:
                w, h = self._screen_size()
                raw = [(Rect(0, 0, w, h), True, f"screen{self._d.get_default_screen()}")]
        raw.sort(key=lambda m: (not m[1], m[0].left, m[0].top))
        return [Monitor(i, rect, scale, i == 0, name) for i, (rect, _, name) in enumerate(raw)]

    def _randr_monitors(self) -> List[Tuple[Rect, bool, str]]:
        randr = self._xl.randr
        out: List[Tuple[Rect, bool, str]] = []
        try:
            reply = randr.get_monitors(self._root, True)
            for m in reply.monitors:
                if m.width_in_pixels <= 0 or m.height_in_pixels <= 0:
                    continue
                try:
                    name = self._d.get_atom_name(m.name)
                except Exception:
                    name = ""
                out.append((Rect(int(m.x), int(m.y), int(m.width_in_pixels), int(m.height_in_pixels)), bool(m.primary), str(name)))
            if out:
                return out
        except Exception:
            out = []
        try:  # RandR < 1.5: walk the active CRTCs
            res = randr.get_screen_resources(self._root)
            ts = res.config_timestamp
            try:
                primary_output = randr.get_output_primary(self._root).output
            except Exception:
                primary_output = 0
            for crtc in res.crtcs:
                info = randr.get_crtc_info(self._d, crtc, ts)
                if not info.mode or info.width <= 0 or info.height <= 0:
                    continue
                name = ""
                if info.outputs:
                    try:
                        name = randr.get_output_info(self._d, info.outputs[0], ts).name
                        if isinstance(name, bytes):
                            name = name.decode("utf-8", "replace")
                    except Exception:
                        name = ""
                out.append((Rect(int(info.x), int(info.y), int(info.width), int(info.height)), primary_output in info.outputs, str(name)))
        except Exception:
            return []
        return out

    def _mss(self) -> Any:
        """This thread's mss grabber (older mss releases are bound to their thread).

        Each grabber has its own X connection. It lives in a thread-local slot,
        so it is closed as soon as its thread exits (or the backend is closed or
        garbage collected); short-lived worker threads therefore never pile up
        X connections.
        """
        holder = getattr(self._mss_local, "holder", None)
        if holder is None:
            import mss  # hard dependency, but keep the import lazy

            # mss >= 10 exposes ``mss.MSS``; older releases only the ``mss.mss`` factory.
            factory = getattr(mss, "MSS", None) or mss.mss
            try:
                sct = factory(display=self.display_name)
            except TypeError:
                if os.environ.get("DISPLAY") != self.display_name:
                    raise
                sct = factory()
            holder = _MssHolder(sct)
            with self._lock:
                closers = self._state.mss_closers
                closers[:] = [c for c in closers if c.alive]
                closers.append(weakref.finalize(holder, _close_quietly, sct))
            self._mss_local.holder = holder
        return holder.sct

    def _drop_mss(self) -> None:
        holder = getattr(self._mss_local, "holder", None)
        if holder is not None:
            del self._mss_local.holder  # the finalizer closes the grabber

    def _grab_mss(self, r: Rect) -> Any:
        from PIL import Image

        shot = self._mss().grab({"left": r.left, "top": r.top, "width": r.width, "height": r.height})
        return Image.frombuffer("RGB", (shot.width, shot.height), bytes(shot.bgra), "raw", "BGRX", 0, 1)

    def _image_format(self, img: Any) -> Tuple[int, int, bool]:
        """``(depth, bits_per_pixel, msb_first)`` of a ``GetImage`` reply (call under the lock)."""
        X = self._xl.X
        depth = int(img.depth)
        bpp = next((f.bits_per_pixel for f in self._d.display.info.pixmap_formats if f.depth == depth), 32)
        msb = int(self._d.display.info.image_byte_order) == X.MSBFirst
        return depth, bpp, msb

    @staticmethod
    def _ximage_to_pil(data: Any, width: int, height: int, depth: int, bpp: int, msb: bool) -> Any:
        """RGB PIL image of ZPixmap ``data`` (``width`` x ``height``) in the given pixel format."""
        from PIL import Image

        data = data if isinstance(data, bytes) else bytes(data)
        stride = len(data) // height if height else 0
        if bpp == 32:
            return Image.frombuffer("RGB", (width, height), data, "raw", "XRGB" if msb else "BGRX", stride, 1)
        if bpp == 16 and depth == 16 and not msb:
            return Image.frombuffer("RGB", (width, height), data, "raw", "BGR;16", stride, 1)
        if bpp == 24 and not msb:
            return Image.frombuffer("RGB", (width, height), data, "raw", "BGR", stride, 1)
        raise BackendError(f"Unsupported X visual for screenshots (depth {depth}, {bpp} bpp, msb={msb})")

    def _grab_xlib(self, r: Rect) -> Any:
        X = self._xl.X
        with self._lock:
            img = self._root.get_image(r.left, r.top, r.width, r.height, X.ZPixmap, 0xFFFFFFFF)
            depth, bpp, msb = self._image_format(img)
        return self._ximage_to_pil(img.data, r.width, r.height, depth, bpp, msb)

    # ============================================================ window capture
    def _has_composite(self) -> bool:
        try:
            return bool(self._d.has_extension("Composite"))
        except Exception:
            return False

    def _toplevel_ancestor(self, win: Any) -> Any:
        """The root child containing ``win`` (the window manager's frame, or ``win`` itself)."""
        root_id = int(self._root.id)
        cur = win
        for _ in range(32):
            parent = cur.query_tree().parent
            pid = int(getattr(parent, "id", parent) or 0)
            if pid == 0 or pid == root_id:
                return cur
            cur = parent
        return cur

    @_x_errors
    def capture_window(self, handle: Any, *, restore_minimized: bool = False) -> Any:
        """The window's own content from its composited off-screen pixmap
        (``XCompositeNameWindowPixmap`` of its frame, then ``XGetImage``), covered
        and off-screen parts included.

        Needs the Composite extension *and* a running compositing manager (which
        redirects the windows to off-screen pixmaps); without one the X server
        keeps no image of covered areas and :class:`NotSupported` is raised. An
        unmapped (minimized) window has no pixmap at all; ``restore_minimized``
        is not supported on X11 (mapping a window lets the window manager focus it).
        """
        self._check()
        X = self._xl.X
        XError = self._xl.error.XError
        with self._lock:
            win = self._handle(handle)
            if int(win.get_attributes().map_state) != X.IsViewable:
                raise NotSupported(f"X11 window {int(win.id):#x} is not mapped (minimized or hidden): "
                                   "the X server keeps no image of it"
                                   + ("; restore_minimized is not supported on X11" if restore_minimized else ""))
            frame = self._toplevel_ancestor(win)
            if not self._has_composite() or not hasattr(frame, "composite_name_window_pixmap"):
                raise NotSupported("Off-screen window capture needs the X Composite extension")
            fr = self._abs_geometry(frame)
            client = self._abs_geometry(win)
            el, er, et, eb = self._frame_extents(win)
            want = Rect(client.left - el, client.top - et, client.width + el + er, client.height + et + eb)
            try:
                pix = frame.composite_name_window_pixmap()
                try:
                    reply = pix.get_image(0, 0, fr.width, fr.height, X.ZPixmap, 0xFFFFFFFF)
                finally:
                    try:
                        pix.free()
                    except Exception:
                        pass
            except XError as exc:
                raise NotSupported(f"X11 window {int(win.id):#x} has no composited pixmap (is a compositing "
                                   f"manager running?): {exc}") from None
            depth, bpp, msb = self._image_format(reply)
        img = self._ximage_to_pil(reply.data, fr.width, fr.height, depth, bpp, msb)
        part = want.intersect(fr) or fr
        if part != fr:
            img = img.crop((part.left - fr.left, part.top - fr.top, part.right - fr.left, part.bottom - fr.top))
        if is_blank_image(img):
            raise NotSupported(f"X11 window {int(win.id):#x}: the composited pixmap is blank")
        return img

    def _grab(self, r: Rect) -> Any:
        if not self._mss_broken:
            try:
                img = self._grab_mss(r)
            except Exception as exc:
                self._drop_mss()  # a fresh grabber (and connection) is made next time
                self._check()
                if self._screenshot_method == "mss":
                    raise BackendError(f"mss screenshot failed: {exc}") from exc
                # A one-off failure (e.g. the X server's client limit was hit
                # momentarily) only falls back for this grab; give up on mss
                # after repeated failures.
                self._mss_failures += 1
                if self._mss_failures >= 3:
                    self._mss_broken = True
            else:
                self._mss_failures = 0
                return img
        return self._grab_xlib(r)

    @_x_errors
    def screenshot(self, region: Optional[Rect] = None) -> Any:
        """RGB screenshot of ``region`` (default: whole screen); off-screen parts are black."""
        from PIL import Image

        self._check()
        vs = self.virtual_screen()
        r = vs if region is None else Rect.coerce(region)
        if r.width <= 0 or r.height <= 0:
            raise ValueError(f"Screenshot region must have a positive size, got {r}")
        inter = r.intersect(vs)
        if inter is None:
            return Image.new("RGB", (r.width, r.height), (0, 0, 0))
        img = self._grab(inter)
        if inter == r:
            return img.convert("RGB") if img.mode != "RGB" else img
        canvas = Image.new("RGB", (r.width, r.height), (0, 0, 0))
        canvas.paste(img, (inter.left - r.left, inter.top - r.top))
        return canvas
