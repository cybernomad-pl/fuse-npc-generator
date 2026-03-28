#!/usr/bin/env python3
"""
PLAGA '44 -- Fuse file variant generator
Parses .fuse files and creates variants by swapping clothing blocks.
"""

import re
import sys
import struct
from pathlib import Path

FUSE_DIR = Path(__file__).parent
CLOTHS_DIR = Path("/mnt/c/Users/boris/AppData/Local/Mixamo/Fuse/Data/Domains/Mixamo/Cloths")


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
    """Parse all protobuf sections from a .fuse file."""
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

            # Extract item name (after the quote character)
            item_match = re.search(rb'"(.+?)\(', data[next3:actual_end])
            item_name = item_match.group(1).decode('ascii', errors='replace').strip() if item_match else ""

            sections.append({
                'start': pos,
                'end': actual_end,
                'index': idx,
                'name': name,
                'item': item_name,
                'inner_start': next_pos,  # start of inner content (after 0x0a + varint)
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
    """List available clothing from Fuse installation."""
    if not CLOTHS_DIR.exists():
        return []
    items = []
    for d in sorted(CLOTHS_DIR.iterdir()):
        if not d.is_dir():
            continue
        vc = d / "VirtualCloth.txt"
        sp = d / "SubstancePreset.txt"
        if vc.exists() and sp.exists():
            vc_data = vc.read_bytes()
            strings = [s.decode() for s in re.findall(rb'[\x20-\x7e]{4,}', vc_data)]
            slot_name = strings[-1] if strings else "?"
            items.append({
                'dir': d.name,
                'slot': get_slot_type(slot_name),
                'slot_name': slot_name,
                'preset': sp.read_bytes(),
            })
    return items


def strip_preset_header(preset_data):
    """
    SubstancePreset.txt has header: 4 bytes (LE size) + 0x08 <varint> + 0x12...
    We need content starting from 0x12 (the Mixamo_ string field).
    """
    pos = preset_data.find(b'\x12')
    if pos < 0:
        raise ValueError("No 0x12 marker found in preset data")
    return preset_data[pos:]


def build_section(index, preset_data):
    """
    Build a complete protobuf section envelope:
    0x0a <varint:inner_length> <inner_data>

    Where inner_data = 0x08 <varint:index> + content_from_0x12
    Preset has extra header (4b size + 08 varint) that we strip.
    """
    content = strip_preset_header(preset_data)
    inner = b'\x08' + encode_varint(index) + content
    return b'\x0a' + encode_varint(len(inner)) + inner


def build_slot_entry(name, index):
    """Build a field 7 protobuf entry: 0x3a <len> (0x0a <slen> <name> 0x10 <idx>)"""
    name_bytes = name.encode('ascii')
    inner = b'\x0a' + encode_varint(len(name_bytes)) + name_bytes + b'\x10' + encode_varint(index)
    return b'\x3a' + encode_varint(len(inner)) + inner


def rebuild_tail(tail_data, slot_swaps):
    """
    Rebuild tail with updated slot assignments.
    slot_swaps: dict of {section_index: new_item_name}

    Tail structure (top-level protobuf fields):
      field 2 (0x10): varint
      field 3 (0x18): varint
      field 4 (0x20): varint
      field 5 (0x2a): morph targets (repeated)
      field 6 (0x32): unknown (repeated)
      field 7 (0x3a): slot assignments (repeated)
    """
    # Find where field 7 entries start
    pos = 0
    pre_slots = bytearray()

    # Copy everything before first field 7
    while pos < len(tail_data):
        tag = tail_data[pos]
        field = tag >> 3
        wire = tag & 7

        if field == 7 and wire == 2:
            break  # found first slot entry

        if wire == 0:  # varint
            _, next_pos = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:next_pos]
            pos = next_pos
        elif wire == 2:  # length-delimited
            length, next_pos = decode_varint(tail_data, pos + 1)
            pre_slots += tail_data[pos:next_pos + length]
            pos = next_pos + length
        else:
            pre_slots += tail_data[pos:pos+1]
            pos += 1

    # Parse existing slot entries
    slots = []
    while pos < len(tail_data):
        tag = tail_data[pos]
        field = tag >> 3
        wire = tag & 7
        if field != 7 or wire != 2:
            break
        length, next_pos = decode_varint(tail_data, pos + 1)
        entry = tail_data[next_pos:next_pos+length]

        # Parse: field 1 = name, field 2 = index
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

    # Apply swaps
    new_slots = []
    for name, idx in slots:
        if idx in slot_swaps:
            new_slots.append((slot_swaps[idx], idx))
        else:
            new_slots.append((name, idx))

    # Rebuild
    result = bytes(pre_slots)
    for name, idx in new_slots:
        result += build_slot_entry(name, idx)

    # Append anything after slots (shouldn't be anything, but just in case)
    result += tail_data[pos:]

    return result


def create_variant(source_path, output_path, swaps, body_swap=None):
    """
    Create a .fuse variant.

    swaps: dict of {section_index: cloth_dir_name}
    body_swap: 'female' to swap MaleFitA -> FemaleFitA (experimental)
    """
    data = Path(source_path).read_bytes()
    sections = parse_sections(data)

    # File structure:
    # [0-15]   MBA magic header
    # [16-19]  LE32: file_size - 20
    # [20]     0x0a (protobuf wrapper tag)
    # [21-23]  varint: total sections size
    # [24+]    sections data
    # [after sections] tail: morphs + slot assignments
    header = data[:16]  # MBA magic only
    tail = data[sections[-1]['end']:]

    print(f"Source: {source_path} ({len(data)} bytes)")
    print(f"Header: {len(header)} bytes, Tail: {len(tail)} bytes")
    print(f"Sections: {len(sections)}")
    print()

    # Load available cloths
    cloths = {c['dir']: c for c in list_cloths()}

    # Build new sections + collect slot swaps
    new_sections = []
    slot_swaps = {}  # {section_index: new_item_name}
    for sec in sections:
        idx = sec['index']
        if idx in swaps:
            cloth_name = swaps[idx]
            if cloth_name not in cloths:
                print(f"  ERROR: '{cloth_name}' not found in Fuse cloths!")
                print(f"  Available: {', '.join(cloths.keys())}")
                return False
            cloth = cloths[cloth_name]
            new_block = build_section(idx, cloth['preset'])
            print(f"  [{idx}] SWAP: {sec['item']} -> {cloth['slot_name']} ({cloth_name})")
            print(f"       {len(data[sec['start']:sec['end']])} bytes -> {len(new_block)} bytes")
            new_sections.append(new_block)
            slot_swaps[idx] = cloth['slot_name']
        else:
            new_sections.append(data[sec['start']:sec['end']])
            print(f"  [{idx}] KEEP: {sec['item']}")

    # Rebuild tail with proper protobuf slot entries
    if slot_swaps:
        tail = rebuild_tail(tail, slot_swaps)

    # Assemble: header(16) + LE32(size) + 0x0a + varint(sections_size) + sections + tail
    sections_blob = b''.join(new_sections)
    sections_wrapper = b'\x0a' + encode_varint(len(sections_blob)) + sections_blob
    result = header[:16] + b'\x00\x00\x00\x00' + sections_wrapper + tail

    # Optional body swap
    if body_swap == 'female':
        result = result.replace(b'MaleFitA', b'FemaleFitA')  # UWAGA: same length = 8 chars vs 10!
        # Can't do simple replace -- different lengths break protobuf!
        print(f"\n  WARNING: MaleFitA->FemaleFitA changes string lengths!")
        print(f"  This WILL corrupt the file. Use Fuse GUI for body swaps.")
        return False

    # Update size field in header (LE uint32 at offset 16 = file_size - 20)
    new_size = len(result) - 20
    result = result[:16] + struct.pack('<I', new_size) + result[20:]

    Path(output_path).write_bytes(result)
    print(f"\nSaved: {output_path} ({len(result)} bytes, header_size={new_size})")

    # Verify
    new_sections_check = parse_sections(result)
    print(f"Verify: {len(new_sections_check)} sections parsed OK")
    for s in new_sections_check:
        print(f"  [{s['index']}] {s['item']}")

    return True


def analyze(filepath):
    data = Path(filepath).read_bytes()
    sections = parse_sections(data)
    print(f"\n=== {Path(filepath).name} ({len(data)} bytes) ===\n")
    for s in sections:
        slot = get_slot_type(s['item'])
        size = s['end'] - s['start']
        print(f"  [{s['index']}] {slot:10s}  {s['item']:45s}  ({size} bytes)")
    male = len(re.findall(rb'MaleFitA', data))
    female = len(re.findall(rb'FemaleFitA', data))
    print(f"\n  Body: MaleFitA={male}, FemaleFitA={female}")


if __name__ == '__main__':
    print("=== PLAGA '44 Fuse Variant Generator ===\n")

    # Show available cloths
    print("Available clothing:")
    for c in list_cloths():
        print(f"  [{c['slot']:10s}] {c['dir']:30s} -> {c['slot_name']}")

    # Analyze source
    source = FUSE_DIR / "Anglojanek-1.fuse"
    analyze(source)

    # --- CREATE ANGLOJANEK-2 ---
    # Swap top [1] -> chainmailarmor (IOTV Tactical Shirt)
    # Swap beard [4] -> REDBLOOD (Viking Braid)
    print("\n" + "="*60)
    print("GENERATING ANGLOJANEK-2\n")

    ok = create_variant(
        source_path=source,
        output_path=FUSE_DIR / "Anglojanek-2.fuse",
        swaps={
            1: 'chainmailarmor',   # Top -> IOTV Tactical Shirt
            4: 'REDBLOOD',         # Beard -> Viking Braid
        },
    )
    if ok:
        analyze(FUSE_DIR / "Anglojanek-2.fuse")
