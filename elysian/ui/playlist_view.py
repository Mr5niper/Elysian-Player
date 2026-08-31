"""The playlist table.

Rows are keyed by the playlist's stable track id, never by position, so
filtering, sorting and drag-reordering cannot desynchronise the way they did
in v1. Selection is owned here and is never clobbered when the playing track
changes -- another v1 annoyance, where every track change and every filter
keystroke stole your selection and yanked the scroll position.
"""
import os

import dearpygui.dearpygui as dpg

from ..models.track import format_time

ROW_PAYLOAD = "elysian_row"


class PlaylistView:
    def __init__(self, playlist, on_activate, on_reorder, on_context):
        self.playlist = playlist
        self.on_activate = on_activate
        self.on_reorder = on_reorder
        self.on_context = on_context
        self.table = "playlist_table"
        self.selected: set[int] = set()
        self.current_id: int = -1
        self._row_tag: dict[int, str] = {}
        self._filter = ""
        self._sort_key: str | None = None
        self._sort_desc = False

    # ---- construction -------------------------------------------------

    def build(self, parent) -> None:
        with dpg.table(
            tag=self.table,
            parent=parent,
            header_row=True,
            clipper=True,
            resizable=True,
            reorderable=False,
            hideable=False,
            borders_innerH=False,
            borders_outerH=False,
            borders_innerV=False,
            borders_outerV=False,
            row_background=True,
            scrollY=True,
            freeze_rows=1,
            policy=dpg.mvTable_SizingStretchProp,
            height=-1,
            width=-1,
            callback=self._on_sort,
            sortable=True,
        ):
            dpg.add_table_column(label="#", init_width_or_weight=0.06,
                                 no_sort=True)
            dpg.add_table_column(label="Title", init_width_or_weight=0.44,
                                 user_data="title", prefer_sort_ascending=True)
            dpg.add_table_column(label="Artist", init_width_or_weight=0.28,
                                 user_data="artist")
            dpg.add_table_column(label="Album", init_width_or_weight=0.16,
                                 user_data="album")
            dpg.add_table_column(label="Time", init_width_or_weight=0.10,
                                 user_data="duration")

    # ---- rendering ----------------------------------------------------

    def refresh(self) -> None:
        """Rebuild every row. Cheap enough with clipper on, since DPG only
        draws the visible slice."""
        if not dpg.does_item_exist(self.table):
            return
        for child in dpg.get_item_children(self.table, 1) or []:
            dpg.delete_item(child)
        self._row_tag.clear()

        needle = self._filter
        for position, track in enumerate(self.playlist.tracks):
            track_id = self.playlist.id_at(position)
            if needle and not self._matches(track, needle):
                continue
            self._add_row(position, track_id, track)

    def _matches(self, track, needle: str) -> bool:
        name = os.path.basename(track.path)
        haystack = f"{track.title} {track.artist} {track.album} {name}"
        return needle in haystack.lower()

    def _add_row(self, position: int, track_id: int, track) -> None:
        with dpg.table_row(parent=self.table) as row:
            tag = f"row_{track_id}"
            self._row_tag[track_id] = tag
            marker = "\u25B6" if track_id == self.current_id else str(position + 1)
            dpg.add_selectable(
                label=marker,
                tag=tag,
                span_columns=True,
                default_value=track_id in self.selected,
                callback=self._on_click,
                user_data=track_id,
                payload_type=ROW_PAYLOAD,
                drag_callback=self._on_drag,
                drop_callback=self._on_drop,
            )
            with dpg.drag_payload(parent=tag, drag_data=track_id,
                                  payload_type=ROW_PAYLOAD):
                dpg.add_text(track.title[:48])
            dpg.add_text(track.title)
            dpg.add_text(track.artist)
            dpg.add_text(track.album)
            dpg.add_text(format_time(track.length))
        return row

    # ---- events -------------------------------------------------------

    def _on_click(self, sender, value, track_id):
        ctrl = dpg.is_key_down(dpg.mvKey_ModCtrl)
        if not ctrl:
            for other in self.selected:
                tag = self._row_tag.get(other)
                if tag and dpg.does_item_exist(tag) and other != track_id:
                    dpg.set_value(tag, False)
            self.selected = {track_id} if value else set()
        else:
            if value:
                self.selected.add(track_id)
            else:
                self.selected.discard(track_id)
        dpg.set_value(sender, track_id in self.selected)

    def _on_drag(self, sender, value, user_data):
        pass

    def _on_drop(self, sender, drag_data, user_data):
        target_id = dpg.get_item_user_data(sender)
        if isinstance(drag_data, int) and target_id != drag_data:
            self.on_reorder(drag_data, target_id)

    def _on_sort(self, sender, sort_specs):
        if not sort_specs:
            return
        column, direction = sort_specs[0]
        key = dpg.get_item_user_data(column)
        if not key:
            return
        self._sort_key = key
        self._sort_desc = direction < 0
        self.playlist.sort_by(key, reverse=self._sort_desc)
        self.refresh()

    def activate_selected(self) -> None:
        if self.selected:
            self.on_activate(next(iter(self.selected)))

    # ---- external state ------------------------------------------------

    def set_filter(self, text: str) -> None:
        self._filter = (text or "").strip().lower()
        self.refresh()

    def set_current(self, track_id: int) -> None:
        """Update the playing marker without touching selection or scroll."""
        previous = self.current_id
        self.current_id = track_id
        for tid in (previous, track_id):
            tag = self._row_tag.get(tid)
            if not tag or not dpg.does_item_exist(tag):
                continue
            position = self.playlist.index_of(tid)
            label = "\u25B6" if tid == track_id else str(position + 1)
            dpg.configure_item(tag, label=label)

    def scroll_to_current(self) -> None:
        tag = self._row_tag.get(self.current_id)
        if tag and dpg.does_item_exist(tag):
            try:
                dpg.set_y_scroll(self.table, dpg.get_item_pos(tag)[1])
            except Exception:
                pass

    def visible_ids(self) -> list[int]:
        return list(self._row_tag.keys())

    def select_only(self, track_id: int) -> None:
        for tid, tag in self._row_tag.items():
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, tid == track_id)
        self.selected = {track_id}
