"""Utils for data/MIDI processing."""
import hashlib
import json
import re
import logging
import pathlib
import mido

from collections import defaultdict

from mido.midifiles.units import tick2second
from aria.config import load_config


# TODO:
# - Possibly refactor names 'mid' to 'midi'


class MidiDict:
    """Container for MIDI data in dictionary form.

    The arguments must contain messages of the following format:

    meta_msg:
    {
        "type": "text" or "copyright",
        "data": str,
    }

    tempo_msg:
    {
        "type": "tempo",
        "data": int,
        "tick": int,
    }

    pedal_msg:
    {
        "type": "pedal",
        "data": 1 if pedal on; 0 if pedal off,
        "tick": int,
        "channel": int,
    }

    instrument_msg:
    {
        "type": "instrument",
        "data": int,
        "tick": int,
        "channel": int,
    }

    note_msg:
    {
        "type": "note",
        "data": {
            "pitch": int,
            "start": tick of note_on message,
            "end": tick of note_off message,
            "velocity": int,
        },
        "tick": tick of note_on message,
        "channel": int
    }

    Args:
        meta_msgs (list): Text or copyright MIDI meta messages.
        tempo_msgs (list): Tempo messages corresponding to the set_tempo MIDI
            message.
        pedal_msgs (list): Pedal on/off messages corresponding to control
            change 64 MIDI messages.
        instrument_msgs (list): Instrument messages corresponding to program
            change MIDI messages
        note_msgs (list): Note messages corresponding to matching note-on and
            note-off MIDI messages.
    """

    def __init__(
        self,
        meta_msgs: list,
        tempo_msgs: list,
        pedal_msgs: list,
        instrument_msgs: list,
        note_msgs: list,
        ticks_per_beat: int,
        metadata: dict,
    ):
        self.meta_msgs = meta_msgs
        self.tempo_msgs = tempo_msgs
        self.pedal_msgs = pedal_msgs
        self.instrument_msgs = instrument_msgs
        self.note_msgs = note_msgs
        self.ticks_per_beat = ticks_per_beat
        self.metadata = metadata

        # Special case that temo_msg is empty, in this case we spoof the default
        if not self.tempo_msgs:
            self.tempo_msgs = [
                {
                    "type": "tempo",
                    "data": 500000,
                    "tick": 0,
                }
            ]

        if not self.instrument_msgs:
            self.instrument_msgs = [
                {
                    "type": "instrument",
                    "data": 0,
                    "tick": 0,
                    "channel": 0,
                }
            ]

    @classmethod
    @property
    def program_to_instrument(cls):
        # This combines the individual dictionaries into one
        return (
            {i: "piano" for i in range(0, 7 + 1)}
            | {i: "chromatic" for i in range(8, 15 + 1)}
            | {i: "organ" for i in range(16, 23 + 1)}
            | {i: "guitar" for i in range(24, 31 + 1)}
            | {i: "bass" for i in range(32, 39 + 1)}
            | {i: "strings" for i in range(40, 47 + 1)}
            | {i: "ensemble" for i in range(48, 55 + 1)}
            | {i: "brass" for i in range(56, 63 + 1)}
            | {i: "reed" for i in range(64, 71 + 1)}
            | {i: "pipe" for i in range(72, 79 + 1)}
            | {i: "synth_lead" for i in range(80, 87 + 1)}
            | {i: "synth_pad" for i in range(88, 95 + 1)}
            | {i: "synth_effect" for i in range(96, 103 + 1)}
            | {i: "ethnic" for i in range(104, 111 + 1)}
            | {i: "percussive" for i in range(112, 119 + 1)}
            | {i: "sfx" for i in range(120, 127 + 1)}
        )

    def get_msg_dict(self):
        return {
            "meta_msgs": self.meta_msgs,
            "tempo_msgs": self.tempo_msgs,
            "pedal_msgs": self.pedal_msgs,
            "instrument_msgs": self.instrument_msgs,
            "note_msgs": self.note_msgs,
            "ticks_per_beat": self.ticks_per_beat,
            "metadata": self.metadata,
        }

    def to_midi(self):
        """Inplace version of dict_to_midi."""
        return dict_to_midi(self.get_msg_dict())

    @classmethod
    def from_msg_dict(cls, msg_dict: dict):
        """Inplace version of midi_to_dict."""
        assert msg_dict.keys() == {
            "meta_msgs",
            "tempo_msgs",
            "pedal_msgs",
            "instrument_msgs",
            "note_msgs",
            "ticks_per_beat",
            "metadata",
        }

        return cls(**msg_dict)

    @classmethod
    def from_midi(cls, mid_path: str):
        """Loads a MIDI file and returns the coresponding MidiDict."""
        mid = mido.MidiFile(mid_path)
        return cls(**midi_to_dict(mid))

    def calculate_hash(self):
        msg_dict_to_hash = self.get_msg_dict()
        # Remove meta when calculating hash
        msg_dict_to_hash.pop("meta_msgs")
        msg_dict_to_hash.pop("metadata")

        return hashlib.md5(
            json.dumps(msg_dict_to_hash, sort_keys=True).encode()
        ).hexdigest()

    # TODO:
    # - Add remove drums (aka remove channel 9) pre-processing
    # - Add similar method for removing specific programs
    # - Decide whether this is necessary to have here in pre-precessing
    def remove_instruments(self, config: dict):
        """Removes all messages with instruments specified in config, excluding
        drums."""
        programs_to_remove = [
            i
            for i in range(1, 127 + 1)
            if config[self.program_to_instrument[i]] is True
        ]
        channels_to_remove = [
            msg["channel"]
            for msg in self.instrument_msgs
            if msg["data"] in programs_to_remove
        ]

        # Remove drums (channel 9) from channels to remove
        channels_to_remove = [i for i in channels_to_remove if i != 9]

        # Remove unwanted messages all type by looping over msgs types. We need
        # to remove ticks_per_beat as this does not contain any messages.
        msg_dict = {
            k: v
            for k, v in self.get_msg_dict().items()
            if k != "ticks_per_beat" and k != "metadata"
        }
        for msgs_name, msgs_list in msg_dict.items():
            setattr(
                self,
                msgs_name,
                [
                    msg
                    for msg in msgs_list
                    if msg.get("channel", -1) not in channels_to_remove
                ],
            )


# Loosely inspired by pretty_midi
def _extract_track_data(track: mido.MidiTrack):
    meta_msgs = []
    tempo_msgs = []
    pedal_msgs = []
    instrument_msgs = []
    note_msgs = []

    last_note_on = defaultdict(list)
    for message in track:
        # Meta messages
        if message.is_meta is True:
            if message.type == "text" or message.type == "copyright":
                meta_msgs.append(
                    {
                        "type": message.type,
                        "data": message.text,
                    }
                )
            # Tempo messages
            elif message.type == "set_tempo":
                tempo_msgs.append(
                    {
                        "type": "tempo",
                        "data": message.tempo,
                        "tick": message.time,
                    }
                )
        # Instrument messages
        elif message.type == "program_change":
            instrument_msgs.append(
                {
                    "type": "instrument",
                    "data": message.program,
                    "tick": message.time,
                    "channel": message.channel,
                }
            )
        # Pedal messages
        elif message.type == "control_change" and message.control == 64:
            if message.value == 0:
                val = 0
            else:
                val = 1
            pedal_msgs.append(
                {
                    "type": "pedal",
                    "data": val,
                    "tick": message.time,
                    "channel": message.channel,
                }
            )
        # Note messages
        elif message.type == "note_on" and message.velocity > 0:
            last_note_on[(message.note, message.channel)].append(
                (message.time, message.velocity)
            )
        elif message.type == "note_off" or (
            message.type == "note_on" and message.velocity == 0
        ):
            # Ignore non-existent note-ons
            if (message.note, message.channel) in last_note_on:
                end_tick = message.time
                open_notes = last_note_on[(message.note, message.channel)]

                notes_to_close = [
                    (start_tick, velocity)
                    for start_tick, velocity in open_notes
                    if start_tick != end_tick
                ]
                notes_to_keep = [
                    (start_tick, velocity)
                    for start_tick, velocity in open_notes
                    if start_tick == end_tick
                ]

                for start_tick, velocity in notes_to_close:
                    note_msgs.append(
                        {
                            "type": "note",
                            "data": {
                                "pitch": message.note,
                                "start": start_tick,
                                "end": end_tick,
                                "velocity": velocity,
                            },
                            "tick": start_tick,
                            "channel": message.channel,
                        }
                    )

                if len(notes_to_close) > 0 and len(notes_to_keep) > 0:
                    # Note-on on the same tick but we already closed
                    # some previous notes -> it will continue, keep it.
                    last_note_on[
                        (message.note, message.channel)
                    ] = notes_to_keep
                else:
                    # Remove the last note on for this instrument
                    del last_note_on[(message.note, message.channel)]

    return {
        "meta_msgs": meta_msgs,
        "tempo_msgs": tempo_msgs,
        "pedal_msgs": pedal_msgs,
        "instrument_msgs": instrument_msgs,
        "note_msgs": note_msgs,
    }


def midi_to_dict(mid: mido.MidiFile):
    """Returns MIDI data in an intermediate dictionary form for tokenization.

    Args:
        mid (mido.MidiFile, optional): MIDI to parse.

    Returns:
        dict: Data extracted from the MIDI file. This dictionary has the
            entries "meta_msgs", "tempo_msgs", "pedal_msgs", "instrument_msgs",
            "note_msgs", "ticks_per_beat".
    """
    metadata_config = load_config()["data"]["metadata"]
    # Convert time in mid to absolute
    for track in mid.tracks:
        curr_tick = 0
        for message in track:
            message.time += curr_tick
            curr_tick = message.time

    # Compile track data
    data = defaultdict(list)
    for mid_track in mid.tracks:
        for k, v in _extract_track_data(mid_track).items():
            data[k] += v

    # Sort by tick
    for k, v in data.items():
        data[k] = sorted(v, key=lambda x: x.get("tick", 0))

    # Add ticks per beat
    data["ticks_per_beat"] = mid.ticks_per_beat

    # Add callbacks according to config here
    data["metadata"] = {}
    for process_name, process_config in metadata_config.items():
        if process_config["run"] is True:
            metadata_fn = get_metadata_fn(process_name)
            fn_args = process_config["args"]

            collected_metadata = metadata_fn(mid=mid, msg_data=data, **fn_args)
            if collected_metadata:
                for k, v in collected_metadata.items():
                    data["metadata"][k] = v

    return data


def dict_to_midi(mid_data: dict):
    """Converts MIDI information from dictionary form into a mido.MidiFile.

    This function performs midi_to_dict in reverse. For a complete
    description of the correct input format, see the attributes of the MidiDict
    class.

    Args:
        mid_data (dict): MIDI information in dictionary form. See midi_to_dict
            for a complete description.

    Returns:
        mido.MidiFile: The MIDI parsed from the input data.
    """

    # Magic sorting function
    def _sort_fn(msg):
        if hasattr(msg, "velocity"):
            return (msg.time, msg.velocity)
        else:
            return (msg.time, 1000)

    assert mid_data.keys() == {
        "meta_msgs",
        "tempo_msgs",
        "pedal_msgs",
        "instrument_msgs",
        "note_msgs",
        "ticks_per_beat",
        "metadata",
    }, "Invalid json/dict."

    ticks_per_beat = mid_data.pop("ticks_per_beat")
    mid_data = {
        k: v for k, v in mid_data.items() if k not in {"meta_msgs", "metadata"}
    }

    # Add all messages (not ordered) to one track
    track = mido.MidiTrack()
    end_msgs = defaultdict(list)
    for msg_list in mid_data.values():
        for msg in msg_list:
            if msg["type"] == "tempo":
                track.append(
                    mido.MetaMessage(
                        "set_tempo", tempo=msg["data"], time=msg["tick"]
                    )
                )
            elif msg["type"] == "pedal":
                track.append(
                    mido.Message(
                        "control_change",
                        control=64,
                        value=msg["data"] * 127,  # Stored in dict as 1 or 0
                        channel=msg["channel"],
                        time=msg["tick"],
                    )
                )
            elif msg["type"] == "instrument":
                track.append(
                    mido.Message(
                        "program_change",
                        program=msg["data"],
                        channel=msg["channel"],
                        time=msg["tick"],
                    )
                )
            elif msg["type"] == "note":
                # Note on
                track.append(
                    mido.Message(
                        "note_on",
                        note=msg["data"]["pitch"],
                        velocity=msg["data"]["velocity"],
                        channel=msg["channel"],
                        time=msg["data"]["start"],
                    )
                )
                # Note off
                end_msgs[(msg["channel"], msg["data"]["pitch"])].append(
                    (msg["data"]["start"], msg["data"]["end"])
                )

    # Only add end messages that don't interfere with other notes
    for k, v in end_msgs.items():
        channel, pitch = k
        for start, end in v:
            add = True
            for _start, _end in v:
                if start < _start < end < _end:
                    add = False

            if add is True:
                track.append(
                    mido.Message(
                        "note_on",
                        note=pitch,
                        velocity=0,
                        channel=channel,
                        time=end,
                    )
                )

    # Sort and convert from abs_time -> delta_time
    track = sorted(track, key=_sort_fn)
    tick = 0
    for msg in track:
        msg.time -= tick
        tick += msg.time

    track.append(mido.MetaMessage("end_of_track", time=0))
    mid = mido.MidiFile(type=0)
    mid.ticks_per_beat = ticks_per_beat
    mid.tracks.append(track)

    return mid


def get_duration_ms(
    start_tick: int,
    end_tick: int,
    tempo_msgs: list,
    ticks_per_beat: int,
):
    """Calculates elapsed time between start_tick and end_tick (in ms)"""

    # Finds idx such that:
    # tempo_msg[idx]["tick"] < start_tick <= tempo_msg[idx+1]["tick"]
    for idx, curr_msg in enumerate(tempo_msgs):
        if start_tick <= curr_msg["tick"]:
            break
    if idx > 0:  # Special case idx == 0 -> Don't -1
        idx -= 1

    # It is important that we initialise curr_tick & curr_tempo here. In the
    # case that there is a single tempo message the following loop will not run.
    duration = 0.0
    curr_tick = start_tick
    curr_tempo = tempo_msgs[idx]["data"]

    # Sums all tempo intervals before tempo_msgs[-1]["tick"]
    for curr_msg, next_msg in zip(tempo_msgs[idx:], tempo_msgs[idx + 1 :]):
        curr_tempo = curr_msg["data"]
        if end_tick < next_msg["tick"]:
            delta_tick = end_tick - curr_tick
        else:
            delta_tick = next_msg["tick"] - curr_tick

        duration += tick2second(
            tick=delta_tick,
            tempo=curr_tempo,
            ticks_per_beat=ticks_per_beat,
        )

        if end_tick < next_msg["tick"]:
            break
        else:
            curr_tick = next_msg["tick"]

    # Case end_tick > tempo_msgs[-1]["tick"]
    if end_tick > tempo_msgs[-1]["tick"]:
        curr_tempo = tempo_msgs[-1]["data"]
        delta_tick = end_tick - curr_tick

        duration += tick2second(
            tick=delta_tick,
            tempo=curr_tempo,
            ticks_per_beat=ticks_per_beat,
        )

    # Convert from seconds to milliseconds
    duration = duration * 1e3
    duration = round(duration)

    return duration


def _match_composer(text: str, composer_name: str):
    # If name="bach" this pattern will match "bach", "Bach" or "BACH" if
    # it is either proceeded or preceded by a "_" or " ".
    pattern = (
        r"(^|[\s_])("
        + composer_name.lower()
        + r"|"
        + composer_name.upper()
        + r"|"
        + composer_name.capitalize()
        + r")([\s_]|$)"
    )

    if re.search(pattern, text, re.IGNORECASE):
        return True
    else:
        return False


def meta_composer_filename(
    mid: mido.MidiFile, msg_data: dict, composer_names: list
):
    file_name = pathlib.Path(mid.filename).stem
    matched_names = set()
    for name in composer_names:
        if _match_composer(file_name, name):
            matched_names.add(name)

    # Only return data if only one composer is found
    matched_names = list(matched_names)
    if len(matched_names) == 1:
        return {"composer": matched_names[0]}
    else:
        return {}


def meta_composer_metamsg(
    mid: mido.MidiFile, msg_data: dict, composer_names: list
):
    matched_names = set()
    for msg in msg_data["meta_msgs"]:
        for name in composer_names:
            if _match_composer(msg["data"], name):
                matched_names.add(name)

    # Only return data if only one composer is found
    matched_names = list(matched_names)
    if len(matched_names) == 1:
        return {"composer": matched_names[0]}
    else:
        return {}


def get_metadata_fn(metadata_proc_name: str):
    # Add additional test_names to this inventory
    name_to_fn = {
        "composer_filename": meta_composer_filename,
        "composer_metamsg": meta_composer_metamsg,
    }

    fn = name_to_fn.get(metadata_proc_name, None)
    if fn is None:
        logging.error(
            f"Error finding metadata function for {metadata_proc_name}"
        )
    else:
        return fn


def test_max_programs(midi_dict: MidiDict, max: int):
    """Returns false if midi_dict uses more than {max} programs."""
    present_programs = set(
        map(
            lambda msg: msg["data"],
            midi_dict.instrument_msgs,
        )
    )

    if len(present_programs) <= max:
        return True, len(present_programs)
    else:
        return False, len(present_programs)


def test_max_instruments(midi_dict: MidiDict, max: int):
    present_instruments = set(
        map(
            lambda msg: midi_dict.program_to_instrument[msg["data"]],
            midi_dict.instrument_msgs,
        )
    )

    if len(present_instruments) <= max:
        return True, len(present_instruments)
    else:
        return False, len(present_instruments)


def test_note_frequency(
    midi_dict: MidiDict, max_per_second: float, min_per_second: float
):
    if not midi_dict.note_msgs:
        return False, 0.0

    num_notes = len(midi_dict.note_msgs)
    total_duration_ms = get_duration_ms(
        start_tick=midi_dict.note_msgs[0]["data"]["start"],
        end_tick=midi_dict.note_msgs[-1]["data"]["end"],
        tempo_msgs=midi_dict.tempo_msgs,
        ticks_per_beat=midi_dict.ticks_per_beat,
    )

    if total_duration_ms == 0:
        return False, 0.0

    notes_per_second = (num_notes * 1e3) / total_duration_ms

    if notes_per_second < min_per_second or notes_per_second > max_per_second:
        return False, notes_per_second
    else:
        return True, notes_per_second


def test_note_frequency_per_instrument(
    midi_dict: MidiDict, max_per_second: float, min_per_second: float
):
    num_instruments = len(
        set(
            map(
                lambda msg: midi_dict.program_to_instrument[msg["data"]],
                midi_dict.instrument_msgs,
            )
        )
    )

    if not midi_dict.note_msgs:
        return False, 0.0

    num_notes = len(midi_dict.note_msgs)
    total_duration_ms = get_duration_ms(
        start_tick=midi_dict.note_msgs[0]["data"]["start"],
        end_tick=midi_dict.note_msgs[-1]["data"]["end"],
        tempo_msgs=midi_dict.tempo_msgs,
        ticks_per_beat=midi_dict.ticks_per_beat,
    )

    if total_duration_ms == 0:
        return False, 0.0

    notes_per_second = (num_notes * 1e3) / total_duration_ms

    note_freq_per_instrument = notes_per_second / num_instruments
    if (
        note_freq_per_instrument < min_per_second
        or note_freq_per_instrument > max_per_second
    ):
        return False, note_freq_per_instrument
    else:
        return True, note_freq_per_instrument


def test_min_length(midi_dict: MidiDict, min_seconds: int):
    if not midi_dict.note_msgs:
        return False, 0.0

    total_duration_ms = get_duration_ms(
        start_tick=midi_dict.note_msgs[0]["data"]["start"],
        end_tick=midi_dict.note_msgs[-1]["data"]["end"],
        tempo_msgs=midi_dict.tempo_msgs,
        ticks_per_beat=midi_dict.ticks_per_beat,
    )

    if total_duration_ms / 1e3 < min_seconds:
        return False, total_duration_ms / 1e3
    else:
        return True, total_duration_ms / 1e3


def get_test_fn(test_name: str):
    # Add additional test_names to this inventory
    name_to_fn = {
        "max_programs": test_max_programs,
        "max_instruments": test_max_instruments,
        "total_note_frequency": test_note_frequency,
        "note_frequency_per_instrument": test_note_frequency_per_instrument,
        "min_length": test_min_length,
    }

    fn = name_to_fn.get(test_name, None)
    if fn is None:
        logging.error(f"Error finding preprocessing function for {test_name}")
    else:
        return fn
