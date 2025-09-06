# Project: Kivy Advanced Android File Manager
# Files in this single document:
# 1) main.py  — Kivy app code (advanced features)
# 2) filemanager.kv — KV language UI
# 3) buildozer.spec — essential sections to build APK
# ---------------------------------------------------

# =====================
# 1) main.py
# =====================

from __future__ import annotations
import os
import stat
import shutil
import time
import mimetypes
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Set

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import (StringProperty, BooleanProperty, ListProperty,
                             NumericProperty, ObjectProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.behaviors import FocusBehavior
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.popup import Popup
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.progressbar import ProgressBar
from kivy.core.window import Window

# Android-specific (guarded for desktop dev)
try:
    from android.permissions import request_permissions, Permission, check_permission
    from jnius import autoclass, cast
    from android.storage import primary_external_storage_path
    ANDROID = True
except Exception:
    ANDROID = False

# ---------------------
# Data Models
# ---------------------
@dataclass
class FMItem:
    path: str
    is_dir: bool
    size: int
    mtime: float

    @property
    def name(self):
        return os.path.basename(self.path) or self.path

    @property
    def nice_mtime(self):
        return time.strftime('%Y-%m-%d %H:%M', time.localtime(self.mtime))

    @property
    def nice_size(self):
        if self.is_dir:
            return '<DIR>'
        size = float(self.size)
        for unit in ['B','KB','MB','GB','TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

# ---------------------
# Utility
# ---------------------

def safe_join(base: str, child: str) -> str:
    p = os.path.normpath(os.path.join(base, child))
    return p

def list_dir(path: str, include_hidden: bool) -> List[FMItem]:
    items: List[FMItem] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if not include_hidden and e.name.startswith('.'):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                    items.append(FMItem(
                        path=e.path,
                        is_dir=e.is_dir(follow_symlinks=False),
                        size=0 if e.is_dir(follow_symlinks=False) else st.st_size,
                        mtime=st.st_mtime,
                    ))
                except Exception:
                    # skip entries we can't stat
                    continue
    except Exception as ex:
        raise ex
    return items

# ---------------------
# Popups / dialogs (simple, no external deps)
# ---------------------
class ConfirmPopup(Popup):
    def __init__(self, title: str, message: str, on_yes, **kwargs):
        super().__init__(title=title, size_hint=(0.85, 0.35), **kwargs)
        box = BoxLayout(orientation='vertical', padding=10, spacing=10)
        box.add_widget(Label(text=message))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=10)
        btn_no = Button(text='Cancel')
        btn_yes = Button(text='Yes', bold=True)
        btn_no.bind(on_release=lambda *_: self.dismiss())
        btn_yes.bind(on_release=lambda *_: (self.dismiss(), on_yes()))
        row.add_widget(btn_no); row.add_widget(btn_yes)
        box.add_widget(row)
        self.add_widget(box)

class InputPopup(Popup):
    def __init__(self, title: str, placeholder: str, default: str, on_ok, **kwargs):
        super().__init__(title=title, size_hint=(0.85, 0.40), **kwargs)
        self.on_ok = on_ok
        box = BoxLayout(orientation='vertical', padding=10, spacing=10)
        self.ti = TextInput(text=default, hint_text=placeholder, multiline=False)
        box.add_widget(self.ti)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=10)
        btn_cancel = Button(text='Cancel')
        btn_ok = Button(text='OK', bold=True)
        btn_cancel.bind(on_release=lambda *_: self.dismiss())
        btn_ok.bind(on_release=self._ok)
        row.add_widget(btn_cancel); row.add_widget(btn_ok)
        box.add_widget(row)
        self.add_widget(box)
    def _ok(self, *_):
        val = self.ti.text.strip()
        self.dismiss()
        self.on_ok(val)

# ---------------------
# Views
# ---------------------
class FileRow(RecycleDataViewBehavior, BoxLayout):
    index = NumericProperty(0)
    name = StringProperty('')
    meta = StringProperty('')
    is_dir = BooleanProperty(False)
    selected = BooleanProperty(False)

    def refresh_view_attrs(self, rv, index, data):
        self.index = index
        self.name = data.get('name', '')
        self.meta = data.get('meta', '')
        self.is_dir = data.get('is_dir', False)
        self.selected = data.get('selected', False)
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            if touch.is_double_tap:
                self.parent.parent.parent.on_item_open(self.index)
            else:
                self.parent.parent.parent.toggle_select(self.index)
            return True
        return super().on_touch_down(touch)

class FileList(RecycleView):
    controller = ObjectProperty(None)

# ---------------------
# Main Root
# ---------------------
class Root(BoxLayout):
    current_path = StringProperty('')
    show_hidden = BooleanProperty(False)
    search_query = StringProperty('')
    sort_key = StringProperty('name')  # name|size|date
    sort_desc = BooleanProperty(False)
    status = StringProperty('Ready')
    selection: Set[str] = set()
    clipboard: List[str] = []  # for copy/move

    rv = ObjectProperty(None)

    def on_kv_post(self, base_widget):
        # Permissions (Android <= 10)
        if ANDROID:
            self._android_request_perms()
            base = primary_external_storage_path()
        else:
            base = os.path.expanduser('~')
        self.navigate_to(base)

    def _android_request_perms(self):
        try:
            needed = [Permission.READ_EXTERNAL_STORAGE, Permission.WRITE_EXTERNAL_STORAGE]
            missing = [p for p in needed if not check_permission(p)]
            if missing:
                request_permissions(missing)
        except Exception:
            pass

    # ------------- Navigation -------------
    def navigate_to(self, path: str):
        try:
            path = os.path.abspath(path)
            if not os.path.exists(path):
                self._toast(f"Path not found: {path}")
                return
            self.current_path = path
            self.selection.clear()
            self.reload()
        except Exception as ex:
            self._toast(str(ex))

    def go_up(self):
        self.navigate_to(os.path.dirname(self.current_path))

    def reload(self):
        try:
            items = list_dir(self.current_path, self.show_hidden)
            # search filter
            q = self.search_query.lower().strip()
            if q:
                items = [i for i in items if q in i.name.lower()]
            # sort
            if self.sort_key == 'name':
                items.sort(key=lambda i: (not i.is_dir, i.name.lower()), reverse=self.sort_desc)
            elif self.sort_key == 'size':
                items.sort(key=lambda i: (not i.is_dir, i.size), reverse=self.sort_desc)
            else:  # date
                items.sort(key=lambda i: (not i.is_dir, i.mtime), reverse=self.sort_desc)

            # fill RV data
            data = []
            for it in items:
                meta = (f"{it.nice_size}  •  {it.nice_mtime}" if not it.is_dir else f"Folder  •  {it.nice_mtime}")
                data.append({
                    'name': it.name,
                    'meta': meta,
                    'is_dir': it.is_dir,
                    'selected': it.path in self.selection,
                    'path': it.path,
                })
            self.rv.data = data
            self.status = f"{len(items)} items"
        except Exception as ex:
            self._toast(str(ex))

    # ------------- Selection -------------
    def toggle_select(self, index: int):
        try:
            path = self.rv.data[index]['path']
            if path in self.selection:
                self.selection.remove(path)
            else:
                self.selection.add(path)
            self.reload()
            self.status = f"Selected: {len(self.selection)}"
        except Exception:
            pass

    def select_all(self):
        self.selection = set(d['path'] for d in self.rv.data)
        self.reload()

    def clear_sel(self):
        self.selection.clear(); self.reload()

    def on_item_open(self, index: int):
        item = self.rv.data[index]
        path = item['path']
        if item['is_dir']:
            self.navigate_to(path)
        else:
            self.open_with_android(path)

    # ------------- Actions -------------
    def new_folder(self):
        def _ok(name):
            if not name:
                return
            dest = os.path.join(self.current_path, name)
            try:
                os.makedirs(dest, exist_ok=False)
                self.reload()
            except Exception as ex:
                self._toast(str(ex))
        InputPopup('New Folder', 'Folder name', 'New Folder', _ok).open()

    def rename_item(self):
        if len(self.selection) != 1:
            self._toast('Select exactly one item to rename.')
            return
        src = next(iter(self.selection))
        def _ok(new_name):
            if not new_name:
                return
            dest = os.path.join(os.path.dirname(src), new_name)
            try:
                os.rename(src, dest)
                self.selection = {dest}
                self.reload()
            except Exception as ex:
                self._toast(str(ex))
        InputPopup('Rename', 'New name', os.path.basename(src), _ok).open()

    def delete_items(self):
        if not self.selection:
            self._toast('Nothing selected')
            return
        def _do():
            for p in list(self.selection):
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p)
                    else:
                        os.remove(p)
                except Exception as ex:
                    self._toast(f"Failed: {os.path.basename(p)} — {ex}")
            self.selection.clear(); self.reload()
        ConfirmPopup('Delete', f'Delete {len(self.selection)} item(s)?', _do).open()

    def copy_to_clipboard(self):
        if not self.selection:
            self._toast('Select files to copy')
            return
        self.clipboard = list(self.selection)
        self._toast(f"Copied {len(self.clipboard)} to clipboard")

    def move_to_here(self):
        if not self.clipboard:
            self._toast('Clipboard empty')
            return
        dest_dir = self.current_path
        for src in list(self.clipboard):
            try:
                base = os.path.basename(src)
                dest = os.path.join(dest_dir, base)
                shutil.move(src, dest)
            except Exception as ex:
                self._toast(f"Move failed: {os.path.basename(src)} — {ex}")
        self.clipboard.clear(); self.reload()

    def paste_here(self):
        if not self.clipboard:
            self._toast('Clipboard empty')
            return
        dest_dir = self.current_path
        for src in list(self.clipboard):
            try:
                base = os.path.basename(src)
                dest = os.path.join(dest_dir, base)
                if os.path.isdir(src):
                    if os.path.exists(dest):
                        dest = self._dedupe_name(dest)
                    shutil.copytree(src, dest)
                else:
                    if os.path.exists(dest):
                        dest = self._dedupe_name(dest)
                    shutil.copy2(src, dest)
            except Exception as ex:
                self._toast(f"Copy failed: {os.path.basename(src)} — {ex}")
        self.reload()

    def _dedupe_name(self, path: str) -> str:
        base, ext = os.path.splitext(path)
        i = 1
        candidate = f"{base} ({i}){ext}"
        while os.path.exists(candidate):
            i += 1
            candidate = f"{base} ({i}){ext}"
        return candidate

    def zip_selection(self):
        if not self.selection:
            self._toast('Select files/folders to zip')
            return
        def _ok(name):
            if not name:
                return
            out_path = os.path.join(self.current_path, name if name.endswith('.zip') else name + '.zip')
            try:
                with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for src in self.selection:
                        if os.path.isdir(src):
                            for root, dirs, files in os.walk(src):
                                for f in files:
                                    abspath = os.path.join(root, f)
                                    arcname = os.path.relpath(abspath, os.path.dirname(src))
                                    zf.write(abspath, arcname)
                        else:
                            zf.write(src, os.path.basename(src))
                self.reload()
            except Exception as ex:
                self._toast(str(ex))
        InputPopup('Zip', 'archive name', 'archive.zip', _ok).open()

    def unzip_here(self):
        if len(self.selection) != 1:
            self._toast('Select exactly one .zip file')
            return
        src = next(iter(self.selection))
        if not src.lower().endswith('.zip'):
            self._toast('Not a .zip file')
            return
        def _ok(target):
            if not target:
                return
            target_dir = os.path.join(self.current_path, target)
            try:
                os.makedirs(target_dir, exist_ok=True)
                with zipfile.ZipFile(src, 'r') as zf:
                    zf.extractall(target_dir)
                self.reload()
            except Exception as ex:
                self._toast(str(ex))
        InputPopup('Unzip', 'folder name', os.path.splitext(os.path.basename(src))[0], _ok).open()

    def props(self):
        if not self.selection:
            self._toast('Select item(s)')
            return
        total_files = 0
        total_size = 0
        for p in self.selection:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    total_files += len(files)
                    for f in files:
                        try:
                            total_size += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
            else:
                total_files += 1
                try:
                    total_size += os.path.getsize(p)
                except Exception:
                    pass
        Popup(title='Properties', content=Label(text=f"Items: {len(self.selection)}\nFiles: {total_files}\nSize: {total_size} bytes"), size_hint=(0.8,0.4)).open()

    # ------------- Android open -------------
    def open_with_android(self, path: str):
        if not ANDROID:
            self._toast('Opening is Android-only in this demo.'); return
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            Intent = autoclass('android.content.Intent')
            File = autoclass('java.io.File')
            Uri = autoclass('android.net.Uri')

            f = File(path)
            uri = Uri.fromFile(f)  # NOTE: for Android 7+ FileProvider is recommended
            mime = mimetypes.guess_type(path)[0] or '*/*'

            intent = Intent()
            intent.setAction(Intent.ACTION_VIEW)
            intent.setDataAndType(uri, mime)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity = PythonActivity.mActivity
            activity.startActivity(intent)
        except Exception as ex:
            self._toast(f"No viewer app found or blocked: {ex}")

    # ------------- UI hooks -------------
    def toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.reload()

    def set_sort(self, key: str):
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            self.sort_desc = False
        self.reload()

    def do_search(self, text: str):
        self.search_query = text
        Clock.schedule_once(lambda *_: self.reload(), 0.05)

    def _toast(self, msg: str):
        # Minimal toast using Popup
        p = Popup(title='', content=Label(text=msg), size_hint=(0.7, 0.25))
        Clock.schedule_once(lambda *_: p.dismiss(), 1.2)
        p.open()

class AdvancedFileManagerApp(App):
    def build(self):
        Window.minimum_width, Window.minimum_height = (360, 600)
        Builder.load_file('filemanager.kv')
        return Root()

if __name__ == '__main__':
    AdvancedFileManagerApp().run()



