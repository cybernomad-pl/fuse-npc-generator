#!/usr/bin/env python3
"""
PLAGA '44 -- NPC Variant Generator GUI
Generates randomized .fuse character variants from a base template.
Select which clothing slots to randomize, pick available items per slot.
"""

import re
import struct
import random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# --- FUSE PARSER (from fuse_variant.py) ---

CLOTHS_DIRS = [
    Path("/mnt/c/Program Files (x86)/Steam/steamapps/common/Fuse/Data/Domains/Mixamo/Cloths"),
    Path("/mnt/c/Users/boris/AppData/Local/Mixamo/Fuse/Data/Domains/Mixamo/Cloths"),
]


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
                'name': name, 'item': item_name, 'inner_start': next_pos,
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
                    'preset_path': sp,
                })
    return items


def strip_preset_header(preset_data):
    pos = preset_data.find(b'\x12')
    if pos < 0:
        raise ValueError("No 0x12 marker found in preset data")
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
            _, next_pos = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:next_pos]
            pos = next_pos
        elif wire == 2:
            length, next_pos = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:next_pos + length]
            pos = next_pos + length
        else:
            pre_slots += tail_data[pos:pos+1]
            pos += 1

    slots = []
    while pos < len(tail_data):
        tag = tail_data[pos]
        field = tag >> 3
        wire = tag & 7
        if field != 7 or wire != 2:
            break
        length, next_pos = decode_varint(tail_data, pos + 1)
        entry = tail_data[next_pos:next_pos+length]
        e_pos = 0
        name = ""
        idx = -1
        while e_pos < len(entry):
            e_tag = entry[e_pos]
            e_wire = e_tag & 7
            if e_wire == 2:
                slen, snext = decode_varint(entry, e_pos + 1)
                name = entry[snext:snext+slen].decode('ascii', errors='replace')
                e_pos = snext + slen
            elif e_wire == 0:
                idx, e_pos = decode_varint(entry, e_pos + 1)
            else:
                break
        slots.append((name, idx))
        pos = next_pos + length

    new_slots = []
    for name, idx in slots:
        if idx in slot_swaps:
            new_slots.append((slot_swaps[idx], idx))
        else:
            new_slots.append((name, idx))

    result = bytes(pre_slots)
    for name, idx in new_slots:
        result += build_slot_entry(name, idx)
    result += tail_data[pos:]
    return result


def randomize_morphs(tail_data, intensity=0.3):
    """Randomize morph target float values in field 5 entries."""
    result = bytearray(tail_data)
    # Find all fixed32 floats (wire type 5, tag byte 0x15) inside field 5 blocks
    # Pattern: 0x15 <4 bytes float>
    count = 0
    for m in re.finditer(rb'\x15', bytes(result)):
        pos = m.start()
        if pos + 5 > len(result):
            continue
        try:
            old_val = struct.unpack_from('<f', result, pos + 1)[0]
            # Only randomize if value is in reasonable range
            if -1.0 <= old_val <= 1.0:
                # Add random offset scaled by intensity
                new_val = max(0.0, min(1.0, old_val + random.gauss(0, intensity)))
                struct.pack_into('<f', result, pos + 1, new_val)
                count += 1
        except:
            pass
    return bytes(result), count


def create_variant(source_path, output_path, swaps, randomize_face=False, face_intensity=0.15):
    data = Path(source_path).read_bytes()
    sections = parse_sections(data)
    cloths = {c['dir']: c for c in list_cloths()}

    header = data[:16]
    tail = data[sections[-1]['end']:]

    new_sections = []
    slot_swaps = {}
    log = []

    for sec in sections:
        idx = sec['index']
        if idx in swaps:
            cloth_name = swaps[idx]
            if cloth_name not in cloths:
                log.append(f"ERROR: '{cloth_name}' not found!")
                return False, log
            cloth = cloths[cloth_name]
            preset_data = cloth['preset_path'].read_bytes()
            new_block = build_section(idx, preset_data)
            log.append(f"[{idx}] {sec['item']} -> {cloth['slot_name']}")
            new_sections.append(new_block)
            slot_swaps[idx] = cloth['slot_name']
        else:
            new_sections.append(data[sec['start']:sec['end']])

    if slot_swaps:
        tail = rebuild_tail(tail, slot_swaps)

    if randomize_face:
        tail, morph_count = randomize_morphs(tail, face_intensity)
        log.append(f"Randomized {morph_count} morph values (intensity={face_intensity:.2f})")

    sections_blob = b''.join(new_sections)
    sections_wrapper = b'\x0a' + encode_varint(len(sections_blob)) + sections_blob
    result = header + b'\x00\x00\x00\x00' + sections_wrapper + tail
    new_size = len(result) - 20
    result = result[:16] + struct.pack('<I', new_size) + result[20:]

    Path(output_path).write_bytes(result)
    log.append(f"Saved: {output_path.name} ({len(result)} bytes)")
    return True, log


# --- SLOT MAP: which section index = which slot ---

SLOT_MAP = {
    0: 'hat',
    1: 'top',
    2: 'glasses',
    3: 'gloves',
    4: 'beard/hair',
    5: 'shoes',
    6: 'moustache',
    7: 'bottom',
    8: 'body',
}


# --- GUI ---

class NPCGeneratorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PLAGA '44 -- NPC Variant Generator")
        self.root.geometry("800x700")
        self.root.configure(bg='#000000')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background='#000000')
        style.configure('TLabel', background='#000000', foreground='#ffffff',
                        font=('Consolas', 10))
        style.configure('TButton', font=('Consolas', 10))
        style.configure('Header.TLabel', font=('Consolas', 14, 'bold'),
                        foreground='#ffffff', background='#000000')
        style.configure('TCheckbutton', background='#000000', foreground='#ffffff',
                        font=('Consolas', 10))
        style.configure('TLabelframe', background='#000000', foreground='#ffffff')
        style.configure('TLabelframe.Label', background='#000000', foreground='#ffffff',
                        font=('Consolas', 10, 'bold'))

        self.cloths = list_cloths()
        self.cloths_by_slot = {}
        for c in self.cloths:
            slot = c['slot']
            if slot not in self.cloths_by_slot:
                self.cloths_by_slot[slot] = []
            self.cloths_by_slot[slot].append(c)

        self.source_path = None
        self.slot_vars = {}  # {slot: {cloth_dir: BooleanVar}}
        self.randomize_slots = {}  # {section_idx: BooleanVar}

        self._build_ui()

    def _build_ui(self):
        # Header
        ttk.Label(self.root, text="PLAGA '44 -- NPC GENERATOR",
                  style='Header.TLabel').pack(pady=(10, 5))

        # Source file
        src_frame = ttk.Frame(self.root)
        src_frame.pack(fill='x', padx=10, pady=5)
        ttk.Label(src_frame, text="Base template:").pack(side='left')
        self.src_label = ttk.Label(src_frame, text="(none)")
        self.src_label.pack(side='left', padx=10)
        ttk.Button(src_frame, text="Browse...", command=self._browse_source).pack(side='right')

        # Auto-load default
        default = Path(__file__).parent / "Anglojanek-1.fuse"
        if default.exists():
            self.source_path = default
            self.src_label.config(text=default.name)

        # Scrollable clothing slots
        canvas_frame = ttk.Frame(self.root)
        canvas_frame.pack(fill='both', expand=True, padx=10, pady=5)

        canvas = tk.Canvas(canvas_frame, bg='#000000', highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient='vertical', command=canvas.yview)
        self.slots_frame = ttk.Frame(canvas)

        self.slots_frame.bind('<Configure>',
            lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=self.slots_frame, anchor='nw')
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

        # Build slot sections
        for sec_idx, slot_name in sorted(SLOT_MAP.items()):
            if sec_idx == 8:  # body -- skip for now
                continue
            self._build_slot_section(sec_idx, slot_name)

        # Face randomization
        face_frame = ttk.LabelFrame(self.slots_frame, text="FACE / BODY MORPHS")
        face_frame.pack(fill='x', pady=5, padx=5)

        self.randomize_face_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(face_frame, text="Randomize face morphs",
                        variable=self.randomize_face_var).pack(anchor='w', padx=10)

        intensity_frame = ttk.Frame(face_frame)
        intensity_frame.pack(fill='x', padx=10, pady=2)
        ttk.Label(intensity_frame, text="Intensity:").pack(side='left')
        self.intensity_var = tk.DoubleVar(value=0.15)
        ttk.Scale(intensity_frame, from_=0.05, to=0.5,
                  variable=self.intensity_var, orient='horizontal').pack(side='left', fill='x', expand=True)

        # Generate controls
        gen_frame = ttk.Frame(self.root)
        gen_frame.pack(fill='x', padx=10, pady=10)

        ttk.Label(gen_frame, text="Count:").pack(side='left')
        self.count_var = tk.IntVar(value=5)
        ttk.Spinbox(gen_frame, from_=1, to=50, textvariable=self.count_var,
                     width=5).pack(side='left', padx=5)

        ttk.Label(gen_frame, text="Prefix:").pack(side='left', padx=(20, 0))
        self.prefix_var = tk.StringVar(value="NPC")
        ttk.Entry(gen_frame, textvariable=self.prefix_var, width=15).pack(side='left', padx=5)

        ttk.Button(gen_frame, text=">>> GENERATE <<<",
                   command=self._generate).pack(side='right', padx=10)

        # Log
        self.log_text = tk.Text(self.root, height=8, bg='#111111', fg='#00ff00',
                                font=('Consolas', 9), insertbackground='#00ff00')
        self.log_text.pack(fill='x', padx=10, pady=(0, 10))

    def _build_slot_section(self, sec_idx, slot_name):
        frame = ttk.LabelFrame(self.slots_frame,
                               text=f"[{sec_idx}] {slot_name.upper()}")
        frame.pack(fill='x', pady=2, padx=5)

        # Randomize checkbox for this slot
        rand_var = tk.BooleanVar(value=False)
        self.randomize_slots[sec_idx] = rand_var
        ttk.Checkbutton(frame, text="Randomize this slot",
                        variable=rand_var).pack(anchor='w', padx=10)

        # Find matching cloths
        matching = []
        for c in self.cloths:
            # Match by slot type -- map slot_name to possible matches
            slot_types = {
                'hat': ['hat'],
                'top': ['top'],
                'bottom': ['bottom'],
                'shoes': ['shoes'],
                'gloves': ['gloves'],
                'glasses': ['glasses', 'mask'],
                'beard/hair': ['beard', 'hair'],
                'moustache': ['moustache'],
            }
            target_types = slot_types.get(slot_name, [slot_name])
            if c['slot'] in target_types or c['slot'] == '?':
                matching.append(c)

        self.slot_vars[sec_idx] = {}
        for c in matching:
            var = tk.BooleanVar(value=True)  # all enabled by default
            self.slot_vars[sec_idx][c['dir']] = var
            ttk.Checkbutton(frame, text=f"{c['dir']} ({c['slot_name']})",
                            variable=var).pack(anchor='w', padx=30)

        if not matching:
            ttk.Label(frame, text="(no matching clothing available)",
                      foreground='#666666').pack(anchor='w', padx=30)

    def _browse_source(self):
        path = filedialog.askopenfilename(
            title="Select base .fuse template",
            filetypes=[("Fuse files", "*.fuse"), ("All files", "*.*")],
            initialdir=str(Path(__file__).parent)
        )
        if path:
            self.source_path = Path(path)
            self.src_label.config(text=self.source_path.name)

    def _log(self, msg):
        self.log_text.insert('end', msg + '\n')
        self.log_text.see('end')
        self.root.update_idletasks()

    def _generate(self):
        if not self.source_path or not self.source_path.exists():
            messagebox.showerror("Error", "Select a base .fuse template first!")
            return

        self.log_text.delete('1.0', 'end')
        count = self.count_var.get()
        prefix = self.prefix_var.get()
        output_dir = self.source_path.parent

        # Collect enabled cloths per randomizable slot
        randomizable = {}
        for sec_idx, rand_var in self.randomize_slots.items():
            if not rand_var.get():
                continue
            enabled = []
            for cloth_dir, var in self.slot_vars.get(sec_idx, {}).items():
                if var.get():
                    enabled.append(cloth_dir)
            if enabled:
                randomizable[sec_idx] = enabled

        if not randomizable and not self.randomize_face_var.get():
            messagebox.showinfo("Info", "Nothing to randomize! Enable at least one slot or face morphs.")
            return

        self._log(f"Generating {count} variants from {self.source_path.name}")
        self._log(f"Randomizing slots: {list(randomizable.keys())}")
        if self.randomize_face_var.get():
            self._log(f"Face morphs: intensity={self.intensity_var.get():.2f}")
        self._log("")

        success = 0
        for i in range(1, count + 1):
            name = f"{prefix}-{i:03d}"
            output = output_dir / f"{name}.fuse"

            # Random swap selection
            swaps = {}
            for sec_idx, options in randomizable.items():
                swaps[sec_idx] = random.choice(options)

            ok, log = create_variant(
                self.source_path, output, swaps,
                randomize_face=self.randomize_face_var.get(),
                face_intensity=self.intensity_var.get(),
            )

            status = "OK" if ok else "FAIL"
            self._log(f"  {name}: {status}")
            for line in log:
                self._log(f"    {line}")

            if ok:
                success += 1

        self._log(f"\nDone: {success}/{count} variants generated in {output_dir}")


if __name__ == '__main__':
    root = tk.Tk()
    app = NPCGeneratorApp(root)
    root.mainloop()
