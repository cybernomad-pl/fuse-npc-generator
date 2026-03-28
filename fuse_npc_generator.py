#!/usr/bin/env python3
"""
PLAGA '44 -- NPC Variant Generator
Step-by-step wizard for batch-generating .fuse character variants.
Reverse-engineered Adobe Fuse CC binary format.
"""

import re
import struct
import random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import platform

# --- PATHS ---

if platform.system() == 'Windows':
    _steam = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Fuse\Data\Domains\Mixamo\Cloths")
    _local = Path(r"C:\Users\boris\AppData\Local\Mixamo\Fuse\Data\Domains\Mixamo\Cloths")
else:
    _steam = Path("/mnt/c/Program Files (x86)/Steam/steamapps/common/Fuse/Data/Domains/Mixamo/Cloths")
    _local = Path("/mnt/c/Users/boris/AppData/Local/Mixamo/Fuse/Data/Domains/Mixamo/Cloths")

CLOTHS_DIRS = [_steam, _local]

# --- COLORS ---

BG       = '#0a0a0a'
BG_CARD  = '#141414'
BG_INPUT = '#1a1a1a'
FG       = '#cccccc'
FG_DIM   = '#666666'
FG_HEAD  = '#ffffff'
ACCENT   = '#e0e0e0'
GREEN    = '#4ec94e'
RED      = '#c94e4e'
BLUE     = '#4e8ec9'
BORDER   = '#333333'


# =========================================================================
#  FUSE BINARY PARSER
# =========================================================================

def decode_varint(data, pos):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        result |= (b & 0x7f) << shift
        pos += 1
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, pos


def encode_varint(value):
    result = bytearray()
    while value > 0x7f:
        result.append((value & 0x7f) | 0x80)
        value >>= 7
    result.append(value & 0x7f)
    return bytes(result)


def parse_sections(data):
    sections = []
    for m in re.finditer(rb'\x0a', data):
        pos = m.start()
        if pos + 3 >= len(data):
            continue
        try:
            length, next_pos = decode_varint(data, pos + 1)
            if next_pos >= len(data) or data[next_pos] != 0x08:
                continue
            idx, next2 = decode_varint(data, next_pos + 1)
            if next2 >= len(data) or data[next2] != 0x12:
                continue
            str_len, next3 = decode_varint(data, next2 + 1)
            if next3 + 7 > len(data) or data[next3:next3+7] != b'Mixamo_':
                continue
            actual_end = next_pos + length
            name = data[next3:next3+str_len].decode('ascii', errors='replace')
            item_match = re.search(rb'"(.+?)\(', data[next3:actual_end])
            item_name = item_match.group(1).decode('ascii', errors='replace').strip() if item_match else ""
            sections.append({
                'start': pos, 'end': actual_end, 'index': idx,
                'name': name, 'item': item_name,
            })
        except (IndexError, ValueError):
            continue
    return sections


def get_slot_type(item_name):
    for prefix, slot in {
        'Hat_': 'hat', 'Top_': 'top', 'Bottom_': 'bottom',
        'Shoes_': 'shoes', 'Gloves_': 'gloves', 'Glasses_': 'glasses',
        'Mask_': 'mask', 'Beard_': 'beard', 'Moustache_': 'moustache',
        'Hair_': 'hair',
    }.items():
        if item_name.startswith(prefix):
            return slot
    return '?'


# Human-readable names
def pretty_name(slot_name):
    """Turn 'Top_MaleFitA_TacticalShirt' into 'Tactical Shirt'."""
    parts = slot_name.split('_')
    # Remove prefix (Top, Hat, etc) and body type (MaleFitA, FemaleFitA, etc)
    clean = []
    skip = {'Top', 'Bottom', 'Hat', 'Shoes', 'Gloves', 'Glasses', 'Mask',
            'Beard', 'Moustache', 'Hair', 'MaleFitA', 'FemaleFitA',
            'MaleFitZombieA', 'TF2Scout', 'TF2Sniper', 'TF2Spy',
            'Alpha', 'B', 'B2', 'C', 'D'}
    for p in parts:
        if p not in skip:
            # CamelCase to spaces
            spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', p)
            clean.append(spaced)
    return ' '.join(clean) if clean else slot_name


def list_cloths():
    items = []
    seen = set()
    for cloths_dir in CLOTHS_DIRS:
        if not cloths_dir.exists():
            continue
        for d in sorted(cloths_dir.iterdir()):
            if not d.is_dir() or d.name in seen:
                continue
            vc = d / "VirtualCloth.txt"
            sp = d / "SubstancePreset.txt"
            if vc.exists() and sp.exists():
                seen.add(d.name)
                vc_data = vc.read_bytes()
                strings = [s.decode() for s in re.findall(rb'[\x20-\x7e]{4,}', vc_data)]
                slot_name = strings[-1] if strings else "?"
                items.append({
                    'dir': d.name,
                    'slot': get_slot_type(slot_name),
                    'slot_name': slot_name,
                    'pretty': pretty_name(slot_name),
                    'preset_path': sp,
                })
    return items


def strip_preset_header(preset_data):
    pos = preset_data.find(b'\x12')
    if pos < 0:
        raise ValueError("No 0x12 marker found")
    return preset_data[pos:]


def build_section(index, preset_data):
    content = strip_preset_header(preset_data)
    inner = b'\x08' + encode_varint(index) + content
    return b'\x0a' + encode_varint(len(inner)) + inner


def build_slot_entry(name, index):
    name_bytes = name.encode('ascii')
    inner = b'\x0a' + encode_varint(len(name_bytes)) + name_bytes + b'\x10' + encode_varint(index)
    return b'\x3a' + encode_varint(len(inner)) + inner


def rebuild_tail(tail_data, slot_swaps):
    pos = 0
    pre_slots = bytearray()
    while pos < len(tail_data):
        tag = tail_data[pos]
        field = tag >> 3
        wire = tag & 7
        if field == 7 and wire == 2:
            break
        if wire == 0:
            _, nxt = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:nxt]; pos = nxt
        elif wire == 2:
            length, nxt = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:nxt + length]; pos = nxt + length
        else:
            pre_slots += tail_data[pos:pos+1]; pos += 1

    slots = []
    while pos < len(tail_data):
        tag = tail_data[pos]
        if (tag >> 3) != 7 or (tag & 7) != 2:
            break
        length, nxt = decode_varint(tail_data, pos + 1)
        entry = tail_data[nxt:nxt+length]
        ep, name, idx = 0, "", -1
        while ep < len(entry):
            ew = entry[ep] & 7
            if ew == 2:
                sl, snp = decode_varint(entry, ep + 1)
                name = entry[snp:snp+sl].decode('ascii', errors='replace'); ep = snp + sl
            elif ew == 0:
                idx, ep = decode_varint(entry, ep + 1)
            else:
                break
        slots.append((name, idx)); pos = nxt + length

    result = bytes(pre_slots)
    for name, idx in slots:
        n = slot_swaps.get(idx, name)
        result += build_slot_entry(n, idx)
    result += tail_data[pos:]
    return result


def randomize_morphs(tail_data, intensity=0.3):
    result = bytearray(tail_data)
    count = 0
    for m in re.finditer(rb'\x15', bytes(result)):
        pos = m.start()
        if pos + 5 > len(result):
            continue
        try:
            v = struct.unpack_from('<f', result, pos + 1)[0]
            if -1.0 <= v <= 1.0:
                nv = max(0.0, min(1.0, v + random.gauss(0, intensity)))
                struct.pack_into('<f', result, pos + 1, nv)
                count += 1
        except:
            pass
    return bytes(result), count


def create_variant(source_path, output_path, swaps, randomize_face=False, face_intensity=0.15):
    data = Path(source_path).read_bytes()
    sections = parse_sections(data)
    cloths_map = {c['dir']: c for c in list_cloths()}

    header = data[:16]
    tail = data[sections[-1]['end']:]

    new_sections, slot_swaps, log = [], {}, []
    for sec in sections:
        idx = sec['index']
        if idx in swaps:
            cn = swaps[idx]
            if cn not in cloths_map:
                log.append(f"ERROR: '{cn}' not found!"); return False, log
            c = cloths_map[cn]
            new_sections.append(build_section(idx, c['preset_path'].read_bytes()))
            slot_swaps[idx] = c['slot_name']
            log.append(f"{sec['item']} -> {c['pretty']}")
        else:
            new_sections.append(data[sec['start']:sec['end']])

    if slot_swaps:
        tail = rebuild_tail(tail, slot_swaps)
    if randomize_face:
        tail, mc = randomize_morphs(tail, face_intensity)
        log.append(f"Face morphs randomized ({mc} values)")

    blob = b''.join(new_sections)
    wrapper = b'\x0a' + encode_varint(len(blob)) + blob
    result = header + b'\x00\x00\x00\x00' + wrapper + tail
    result = result[:16] + struct.pack('<I', len(result) - 20) + result[20:]

    Path(output_path).write_bytes(result)
    log.append(f"-> {output_path.name} ({len(result):,} bytes)")
    return True, log


# =========================================================================
#  SLOT DEFINITIONS
# =========================================================================

SLOT_DEFS = [
    (0, 'hat',        'HAT / HELMET',     ['hat']),
    (1, 'top',        'TOP / JACKET',      ['top']),
    (7, 'bottom',     'PANTS / BOTTOM',    ['bottom']),
    (5, 'shoes',      'SHOES / BOOTS',     ['shoes']),
    (3, 'gloves',     'GLOVES',            ['gloves']),
    (2, 'glasses',    'GLASSES / MASK',    ['glasses', 'mask']),
    (4, 'beard_hair', 'BEARD / HAIR',      ['beard', 'hair']),
    (6, 'moustache',  'MOUSTACHE',         ['moustache']),
]


# =========================================================================
#  WIZARD GUI
# =========================================================================

class WizardApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PLAGA '44 -- NPC Generator")
        self.root.geometry("720x640")
        self.root.configure(bg=BG)
        self.root.resizable(True, True)

        self._setup_styles()

        self.cloths = list_cloths()
        self.source_path = None
        self.template_sections = []

        # Wizard state
        self.slot_vars = {}       # {sec_idx: {cloth_dir: BooleanVar}}
        self.slot_enabled = {}    # {sec_idx: BooleanVar}
        self.face_enabled = tk.BooleanVar(value=True)
        self.face_intensity = tk.DoubleVar(value=0.15)
        self.count_var = tk.IntVar(value=5)
        self.prefix_var = tk.StringVar(value="NPC")

        self.current_step = 0
        self.steps = [
            ("Select Template",    self._build_step_template),
            ("Choose Clothing",    self._build_step_clothing),
            ("Face & Body",        self._build_step_face),
            ("Generate",           self._build_step_generate),
        ]

        # Layout: header + content + nav
        self._build_header()
        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(fill='both', expand=True, padx=20, pady=(0, 10))
        self._build_nav()

        self._show_step(0)

    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('TFrame', background=BG)
        s.configure('TLabel', background=BG, foreground=FG, font=('Segoe UI', 10))
        s.configure('Head.TLabel', background=BG, foreground=FG_HEAD, font=('Segoe UI', 13, 'bold'))
        s.configure('Step.TLabel', background=BG, foreground=FG_DIM, font=('Segoe UI', 9))
        s.configure('StepActive.TLabel', background=BG, foreground=FG_HEAD, font=('Segoe UI', 9, 'bold'))
        s.configure('Card.TFrame', background=BG_CARD)
        s.configure('Card.TLabel', background=BG_CARD, foreground=FG, font=('Segoe UI', 10))
        s.configure('Card.TCheckbutton', background=BG_CARD, foreground=FG, font=('Segoe UI', 10))
        s.configure('CardHead.TLabel', background=BG_CARD, foreground=FG_HEAD, font=('Segoe UI', 11, 'bold'))
        s.configure('Dim.TLabel', background=BG, foreground=FG_DIM, font=('Segoe UI', 9))
        s.configure('TCheckbutton', background=BG, foreground=FG, font=('Segoe UI', 10))

        s.configure('Nav.TButton', font=('Segoe UI', 10), padding=(16, 6))
        s.configure('Go.TButton', font=('Segoe UI', 11, 'bold'), padding=(24, 8))

    # ----- HEADER -----

    def _build_header(self):
        hdr = tk.Frame(self.root, bg=BG, pady=10)
        hdr.pack(fill='x', padx=20)

        tk.Label(hdr, text="PLAGA '44", font=('Consolas', 16, 'bold'),
                 bg=BG, fg=FG_HEAD).pack(anchor='w')
        tk.Label(hdr, text="NPC Variant Generator", font=('Segoe UI', 10),
                 bg=BG, fg=FG_DIM).pack(anchor='w')

        # Step indicator
        self.step_frame = tk.Frame(hdr, bg=BG)
        self.step_frame.pack(fill='x', pady=(12, 0))
        self.step_labels = []
        for i, (name, _) in enumerate(self.steps):
            lbl = tk.Label(self.step_frame, text=f"  {i+1}. {name}  ",
                           font=('Segoe UI', 9), bg=BG, fg=FG_DIM)
            lbl.pack(side='left')
            self.step_labels.append(lbl)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill='x', padx=20)

    def _update_step_indicator(self):
        for i, lbl in enumerate(self.step_labels):
            if i == self.current_step:
                lbl.configure(fg=FG_HEAD, font=('Segoe UI', 9, 'bold'))
            elif i < self.current_step:
                lbl.configure(fg=GREEN, font=('Segoe UI', 9))
            else:
                lbl.configure(fg=FG_DIM, font=('Segoe UI', 9))

    # ----- NAV -----

    def _build_nav(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill='x', padx=20)
        nav = tk.Frame(self.root, bg=BG, pady=10)
        nav.pack(fill='x', padx=20)

        self.btn_back = ttk.Button(nav, text="< Back", style='Nav.TButton', command=self._prev)
        self.btn_back.pack(side='left')

        self.btn_next = ttk.Button(nav, text="Next >", style='Nav.TButton', command=self._next)
        self.btn_next.pack(side='right')

    def _prev(self):
        if self.current_step > 0:
            self._show_step(self.current_step - 1)

    def _next(self):
        # Validate current step
        if self.current_step == 0 and not self.source_path:
            messagebox.showwarning("Template", "Select a .fuse template first.")
            return
        if self.current_step < len(self.steps) - 1:
            self._show_step(self.current_step + 1)

    def _show_step(self, idx):
        self.current_step = idx
        self._update_step_indicator()

        # Clear content
        for w in self.content.winfo_children():
            w.destroy()

        # Build step
        _, builder = self.steps[idx]
        builder()

        # Update nav buttons
        self.btn_back.configure(state='normal' if idx > 0 else 'disabled')
        if idx == len(self.steps) - 1:
            self.btn_next.configure(text="Generate >>>", style='Go.TButton',
                                     command=self._generate)
        else:
            self.btn_next.configure(text="Next >", style='Nav.TButton',
                                     command=self._next)

    # ----- STEP 1: TEMPLATE -----

    def _build_step_template(self):
        f = self.content

        ttk.Label(f, text="Choose a base character template",
                  style='Head.TLabel').pack(anchor='w', pady=(15, 5))
        ttk.Label(f, text="The generator creates variants by swapping clothing and\n"
                          "randomizing facial features on this base character.",
                  style='Dim.TLabel').pack(anchor='w', pady=(0, 15))

        # File picker card
        card = tk.Frame(f, bg=BG_CARD, padx=16, pady=16,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill='x')

        tk.Label(card, text="Template file (.fuse)", font=('Segoe UI', 10, 'bold'),
                 bg=BG_CARD, fg=FG_HEAD).pack(anchor='w')

        row = tk.Frame(card, bg=BG_CARD, pady=8)
        row.pack(fill='x')

        self.src_entry = tk.Entry(row, font=('Consolas', 10), bg=BG_INPUT, fg=FG,
                                   insertbackground=FG, relief='flat', bd=0)
        self.src_entry.pack(side='left', fill='x', expand=True, ipady=4, padx=(0, 8))

        tk.Button(row, text="Browse...", font=('Segoe UI', 9),
                  bg=BG_INPUT, fg=FG, relief='flat', padx=12, pady=2,
                  command=self._browse_template).pack(side='right')

        # Auto-fill if we have a path
        if self.source_path:
            self.src_entry.insert(0, str(self.source_path))
        else:
            default = Path(__file__).parent / "Anglojanek-1.fuse"
            if default.exists():
                self.source_path = default
                self.src_entry.insert(0, str(default))

        # Template info
        self.template_info = tk.Label(card, text="", font=('Segoe UI', 9),
                                       bg=BG_CARD, fg=FG_DIM, justify='left')
        self.template_info.pack(anchor='w', pady=(8, 0))
        self._update_template_info()

        # Available templates
        fuse_dir = Path(__file__).parent
        fuse_files = sorted(fuse_dir.glob("*.fuse"))
        if fuse_files:
            tk.Label(f, text="Available templates:", font=('Segoe UI', 9),
                     bg=BG, fg=FG_DIM).pack(anchor='w', pady=(16, 4))
            for fp in fuse_files:
                btn = tk.Button(f, text=f"  {fp.name}", font=('Consolas', 10),
                                bg=BG_CARD, fg=FG, relief='flat', anchor='w',
                                padx=12, pady=4,
                                command=lambda p=fp: self._select_template(p))
                btn.pack(fill='x', pady=1)

    def _browse_template(self):
        path = filedialog.askopenfilename(
            title="Select .fuse template",
            filetypes=[("Fuse files", "*.fuse"), ("All", "*.*")],
            initialdir=str(Path(__file__).parent))
        if path:
            self._select_template(Path(path))

    def _select_template(self, path):
        self.source_path = path
        self.src_entry.delete(0, 'end')
        self.src_entry.insert(0, str(path))
        self._update_template_info()

    def _update_template_info(self):
        if not self.source_path or not self.source_path.exists():
            self.template_info.configure(text="No file selected")
            return
        try:
            data = self.source_path.read_bytes()
            secs = parse_sections(data)
            self.template_sections = secs
            items = [s['item'] for s in secs if s['item']]
            slots = [f"{get_slot_type(i)}: {pretty_name(i)}" for i in items]
            info = f"{self.source_path.name}  |  {len(data):,} bytes  |  {len(secs)} slots\n"
            info += ", ".join(slots)
            self.template_info.configure(text=info, fg=GREEN)
        except Exception as e:
            self.template_info.configure(text=f"Error: {e}", fg=RED)

    # ----- STEP 2: CLOTHING -----

    def _build_step_clothing(self):
        f = self.content

        ttk.Label(f, text="Select clothing for randomization",
                  style='Head.TLabel').pack(anchor='w', pady=(15, 5))
        ttk.Label(f, text="Enable slots you want to randomize. Check which items to include in the pool.",
                  style='Dim.TLabel').pack(anchor='w', pady=(0, 10))

        # Scrollable area
        canvas = tk.Canvas(f, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(f, orient='vertical', command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)

        inner.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=inner, anchor='nw', tags='inner')
        canvas.configure(yscrollcommand=scrollbar.set)

        # Make inner frame resize with canvas
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure('inner', width=e.width))

        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        for sec_idx, slot_key, label, match_types in SLOT_DEFS:
            self._build_slot_card(inner, sec_idx, label, match_types)

    def _build_slot_card(self, parent, sec_idx, label, match_types):
        card = tk.Frame(parent, bg=BG_CARD, padx=12, pady=8,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill='x', pady=3)

        # Header row with enable toggle
        if sec_idx not in self.slot_enabled:
            self.slot_enabled[sec_idx] = tk.BooleanVar(value=False)

        hdr = tk.Frame(card, bg=BG_CARD)
        hdr.pack(fill='x')

        cb = tk.Checkbutton(hdr, text=label, font=('Segoe UI', 10, 'bold'),
                            variable=self.slot_enabled[sec_idx],
                            bg=BG_CARD, fg=FG_HEAD, selectcolor=BG_INPUT,
                            activebackground=BG_CARD, activeforeground=FG_HEAD)
        cb.pack(side='left')

        # Current item from template
        current = ""
        for s in self.template_sections:
            if s['index'] == sec_idx and s['item']:
                current = pretty_name(s['item'])
                break
        if current:
            tk.Label(hdr, text=f"current: {current}", font=('Segoe UI', 9),
                     bg=BG_CARD, fg=FG_DIM).pack(side='right')

        # Items grid
        matching = [c for c in self.cloths if c['slot'] in match_types or c['slot'] == '?']

        if sec_idx not in self.slot_vars:
            self.slot_vars[sec_idx] = {}

        items_frame = tk.Frame(card, bg=BG_CARD)
        items_frame.pack(fill='x', padx=(20, 0), pady=(4, 0))

        col = 0
        row_frame = tk.Frame(items_frame, bg=BG_CARD)
        row_frame.pack(fill='x')

        for i, c in enumerate(matching):
            if c['dir'] not in self.slot_vars[sec_idx]:
                self.slot_vars[sec_idx][c['dir']] = tk.BooleanVar(value=True)

            cb = tk.Checkbutton(row_frame, text=c['pretty'],
                                variable=self.slot_vars[sec_idx][c['dir']],
                                font=('Segoe UI', 9), bg=BG_CARD, fg=FG,
                                selectcolor=BG_INPUT,
                                activebackground=BG_CARD, activeforeground=FG)
            cb.grid(row=i // 3, column=i % 3, sticky='w', padx=(0, 16))

        if not matching:
            tk.Label(items_frame, text="no items available", font=('Segoe UI', 9),
                     bg=BG_CARD, fg=FG_DIM).pack(anchor='w')

    # ----- STEP 3: FACE -----

    def _build_step_face(self):
        f = self.content

        ttk.Label(f, text="Face & body randomization",
                  style='Head.TLabel').pack(anchor='w', pady=(15, 5))
        ttk.Label(f, text="Slightly randomize facial features and body proportions\n"
                          "to make each NPC look unique.",
                  style='Dim.TLabel').pack(anchor='w', pady=(0, 15))

        card = tk.Frame(f, bg=BG_CARD, padx=16, pady=16,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill='x')

        cb = tk.Checkbutton(card, text="Enable face/body morph randomization",
                            variable=self.face_enabled,
                            font=('Segoe UI', 10, 'bold'),
                            bg=BG_CARD, fg=FG_HEAD, selectcolor=BG_INPUT,
                            activebackground=BG_CARD, activeforeground=FG_HEAD)
        cb.pack(anchor='w')

        tk.Label(card, text="Controls how much facial features vary from the template.\n"
                            "Low = subtle differences. High = very different faces.",
                 font=('Segoe UI', 9), bg=BG_CARD, fg=FG_DIM,
                 justify='left').pack(anchor='w', pady=(8, 12))

        # Intensity slider with labels
        slider_frame = tk.Frame(card, bg=BG_CARD)
        slider_frame.pack(fill='x')

        tk.Label(slider_frame, text="Subtle", font=('Segoe UI', 9),
                 bg=BG_CARD, fg=FG_DIM).pack(side='left')
        tk.Label(slider_frame, text="Extreme", font=('Segoe UI', 9),
                 bg=BG_CARD, fg=FG_DIM).pack(side='right')

        self.intensity_scale = tk.Scale(slider_frame, from_=0.05, to=0.50,
                                         resolution=0.01, orient='horizontal',
                                         variable=self.face_intensity,
                                         bg=BG_CARD, fg=FG, troughcolor=BG_INPUT,
                                         highlightbackground=BG_CARD,
                                         font=('Segoe UI', 9), length=300)
        self.intensity_scale.pack(fill='x', padx=40)

        # Presets
        presets_frame = tk.Frame(card, bg=BG_CARD, pady=8)
        presets_frame.pack(fill='x')

        tk.Label(presets_frame, text="Presets:", font=('Segoe UI', 9),
                 bg=BG_CARD, fg=FG_DIM).pack(side='left', padx=(0, 8))

        for label, val in [("Twins", 0.05), ("Siblings", 0.10), ("Cousins", 0.20), ("Strangers", 0.35), ("Aliens", 0.50)]:
            tk.Button(presets_frame, text=label, font=('Segoe UI', 9),
                      bg=BG_INPUT, fg=FG, relief='flat', padx=8, pady=2,
                      command=lambda v=val: self.face_intensity.set(v)).pack(side='left', padx=2)

    # ----- STEP 4: GENERATE -----

    def _build_step_generate(self):
        f = self.content

        ttk.Label(f, text="Generate variants",
                  style='Head.TLabel').pack(anchor='w', pady=(15, 5))

        # Summary card
        card = tk.Frame(f, bg=BG_CARD, padx=16, pady=12,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill='x', pady=(0, 10))

        tk.Label(card, text="Summary", font=('Segoe UI', 10, 'bold'),
                 bg=BG_CARD, fg=FG_HEAD).pack(anchor='w')

        template_name = self.source_path.name if self.source_path else "?"
        enabled_slots = [SLOT_DEFS[i][2] for i, (si, _, _, _) in enumerate(SLOT_DEFS)
                         if self.slot_enabled.get(si, tk.BooleanVar()).get()]
        summary = f"Template: {template_name}\n"
        summary += f"Randomized slots: {', '.join(enabled_slots) if enabled_slots else 'none'}\n"
        summary += f"Face morphs: {'ON (intensity {:.0%})'.format(self.face_intensity.get()) if self.face_enabled.get() else 'OFF'}"

        tk.Label(card, text=summary, font=('Consolas', 9),
                 bg=BG_CARD, fg=FG, justify='left').pack(anchor='w', pady=(4, 0))

        # Settings
        settings = tk.Frame(f, bg=BG_CARD, padx=16, pady=12,
                            highlightbackground=BORDER, highlightthickness=1)
        settings.pack(fill='x', pady=(0, 10))

        row1 = tk.Frame(settings, bg=BG_CARD)
        row1.pack(fill='x', pady=4)

        tk.Label(row1, text="How many variants:", font=('Segoe UI', 10),
                 bg=BG_CARD, fg=FG).pack(side='left')
        tk.Spinbox(row1, from_=1, to=100, textvariable=self.count_var,
                   width=5, font=('Consolas', 11), bg=BG_INPUT, fg=FG,
                   buttonbackground=BG_INPUT).pack(side='left', padx=8)

        row2 = tk.Frame(settings, bg=BG_CARD)
        row2.pack(fill='x', pady=4)

        tk.Label(row2, text="File name prefix:", font=('Segoe UI', 10),
                 bg=BG_CARD, fg=FG).pack(side='left')
        tk.Entry(row2, textvariable=self.prefix_var, font=('Consolas', 11),
                 bg=BG_INPUT, fg=FG, insertbackground=FG, relief='flat',
                 width=20).pack(side='left', padx=8, ipady=2)

        tk.Label(settings, text="Files will be saved next to the template.",
                 font=('Segoe UI', 9), bg=BG_CARD, fg=FG_DIM).pack(anchor='w', pady=(4, 0))

        # Log output
        self.log_text = tk.Text(f, height=10, bg=BG_INPUT, fg=GREEN,
                                font=('Consolas', 9), insertbackground=GREEN,
                                relief='flat', bd=0)
        self.log_text.pack(fill='both', expand=True, pady=(5, 0))
        self.log_text.insert('1.0', 'Ready. Click "Generate >>>" to start.\n')

    # ----- GENERATE -----

    def _log(self, msg):
        self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.root.update_idletasks()

    def _generate(self):
        if not self.source_path or not self.source_path.exists():
            messagebox.showerror("Error", "No valid template selected.")
            return

        self.log_text.delete('1.0', 'end')
        count = self.count_var.get()
        prefix = self.prefix_var.get()
        output_dir = self.source_path.parent

        # Collect randomizable slots
        randomizable = {}
        for sec_idx, slot_key, label, match_types in SLOT_DEFS:
            if not self.slot_enabled.get(sec_idx, tk.BooleanVar()).get():
                continue
            enabled = [d for d, v in self.slot_vars.get(sec_idx, {}).items() if v.get()]
            if enabled:
                randomizable[sec_idx] = enabled

        if not randomizable and not self.face_enabled.get():
            self._log("Nothing to randomize. Go back and enable some slots.")
            return

        self._log(f"Generating {count} variants from {self.source_path.name}")
        self._log(f"Output: {output_dir}")
        self._log("-" * 50)

        success = 0
        for i in range(1, count + 1):
            name = f"{prefix}-{i:03d}"
            output = output_dir / f"{name}.fuse"

            swaps = {si: random.choice(opts) for si, opts in randomizable.items()}

            ok, log = create_variant(
                self.source_path, output, swaps,
                randomize_face=self.face_enabled.get(),
                face_intensity=self.face_intensity.get(),
            )

            marker = "[OK]" if ok else "[!!]"
            self._log(f"  {marker} {name}")
            for line in log:
                self._log(f"      {line}")

            if ok:
                success += 1

        self._log("-" * 50)
        self._log(f"Done: {success}/{count} generated in {output_dir.name}/")
        self._log("")
        if success == count:
            self._log("All variants created successfully.")
        else:
            self._log(f"WARNING: {count - success} variants failed.")


# =========================================================================
#  MAIN
# =========================================================================

if __name__ == '__main__':
    root = tk.Tk()
    app = WizardApp(root)
    root.mainloop()
