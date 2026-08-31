"""Visual theme for the DearPyGui interface."""
import dearpygui.dearpygui as dpg

BG = (24, 24, 28)
PANEL = (32, 32, 38)
PANEL_HI = (42, 42, 50)
ROW_ALT = (28, 28, 34)
ACCENT = (99, 102, 241)
ACCENT_HI = (124, 127, 255)
TEXT = (228, 228, 235)
TEXT_DIM = (140, 140, 155)
PLAYING = (60, 80, 160)


def _color(target, value, cat=dpg.mvThemeCat_Core):
    dpg.add_theme_color(target, value, category=cat)


def _style(target, x, y=-1.0, cat=dpg.mvThemeCat_Core):
    dpg.add_theme_style(target, x, y, category=cat)


def build_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            _color(dpg.mvThemeCol_WindowBg, BG)
            _color(dpg.mvThemeCol_ChildBg, BG)
            _color(dpg.mvThemeCol_PopupBg, PANEL)
            _color(dpg.mvThemeCol_MenuBarBg, PANEL)
            _color(dpg.mvThemeCol_Border, (52, 52, 62))
            _color(dpg.mvThemeCol_Text, TEXT)
            _color(dpg.mvThemeCol_TextDisabled, TEXT_DIM)

            _color(dpg.mvThemeCol_FrameBg, PANEL)
            _color(dpg.mvThemeCol_FrameBgHovered, PANEL_HI)
            _color(dpg.mvThemeCol_FrameBgActive, PANEL_HI)

            _color(dpg.mvThemeCol_Button, PANEL)
            _color(dpg.mvThemeCol_ButtonHovered, PANEL_HI)
            _color(dpg.mvThemeCol_ButtonActive, ACCENT)

            _color(dpg.mvThemeCol_Header, PLAYING)
            _color(dpg.mvThemeCol_HeaderHovered, PANEL_HI)
            _color(dpg.mvThemeCol_HeaderActive, ACCENT)

            _color(dpg.mvThemeCol_SliderGrab, ACCENT)
            _color(dpg.mvThemeCol_SliderGrabActive, ACCENT_HI)

            _color(dpg.mvThemeCol_ScrollbarBg, BG)
            _color(dpg.mvThemeCol_ScrollbarGrab, PANEL_HI)
            _color(dpg.mvThemeCol_ScrollbarGrabHovered, (60, 60, 72))
            _color(dpg.mvThemeCol_ScrollbarGrabActive, ACCENT)

            _color(dpg.mvThemeCol_TableHeaderBg, PANEL)
            _color(dpg.mvThemeCol_TableRowBg, BG)
            _color(dpg.mvThemeCol_TableRowBgAlt, ROW_ALT)
            _color(dpg.mvThemeCol_TableBorderLight, (44, 44, 54))
            _color(dpg.mvThemeCol_TableBorderStrong, (52, 52, 62))

            _color(dpg.mvThemeCol_Separator, (48, 48, 58))
            _color(dpg.mvThemeCol_TitleBg, PANEL)
            _color(dpg.mvThemeCol_TitleBgActive, PANEL)

            _style(dpg.mvStyleVar_FrameRounding, 6)
            _style(dpg.mvStyleVar_GrabRounding, 6)
            _style(dpg.mvStyleVar_ChildRounding, 8)
            _style(dpg.mvStyleVar_PopupRounding, 8)
            _style(dpg.mvStyleVar_ScrollbarRounding, 8)
            _style(dpg.mvStyleVar_WindowPadding, 14, 14)
            _style(dpg.mvStyleVar_FramePadding, 10, 7)
            _style(dpg.mvStyleVar_ItemSpacing, 9, 8)
            _style(dpg.mvStyleVar_CellPadding, 8, 5)
            _style(dpg.mvStyleVar_ScrollbarSize, 12)
            _style(dpg.mvStyleVar_GrabMinSize, 14)
    return theme


def build_accent_button_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            _color(dpg.mvThemeCol_Button, ACCENT)
            _color(dpg.mvThemeCol_ButtonHovered, ACCENT_HI)
            _color(dpg.mvThemeCol_ButtonActive, ACCENT_HI)
            _color(dpg.mvThemeCol_Text, (255, 255, 255))
            _style(dpg.mvStyleVar_FrameRounding, 8)
    return theme


def build_toggle_on_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            _color(dpg.mvThemeCol_Button, (48, 52, 96))
            _color(dpg.mvThemeCol_ButtonHovered, (58, 62, 112))
            _color(dpg.mvThemeCol_Text, ACCENT_HI)
    return theme


def build_dim_text_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            _color(dpg.mvThemeCol_Text, TEXT_DIM)
    return theme


def build_seek_theme() -> int:
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            _color(dpg.mvThemeCol_FrameBg, (44, 44, 54))
            _color(dpg.mvThemeCol_FrameBgHovered, (52, 52, 64))
            _color(dpg.mvThemeCol_FrameBgActive, (52, 52, 64))
            _color(dpg.mvThemeCol_SliderGrab, ACCENT)
            _color(dpg.mvThemeCol_SliderGrabActive, ACCENT_HI)
            _style(dpg.mvStyleVar_GrabMinSize, 12)
            _style(dpg.mvStyleVar_FrameRounding, 4)
    return theme
