import pytest


@pytest.fixture
def make_toc():
    def build(starts=(0, 5), leadout=11, controls=None, session=1):
        controls = controls or [0] * len(starts)

        def msf(lba):
            minute, rest = divmod(lba + 150, 60 * 75)
            second, frame = divmod(rest, 75)
            return bytes((minute, second, frame))

        def descriptor(point, position, control=0):
            return bytes((session, 0x10 | control, 0, point, 0, 0, 0, 0)) + position

        descriptors = [
            descriptor(0xA0, bytes((1, 0, 0))),
            descriptor(0xA1, bytes((len(starts), 0, 0))),
            descriptor(0xA2, msf(leadout)),
        ]
        descriptors += [
            descriptor(number, msf(start), control)
            for number, (start, control) in enumerate(zip(starts, controls, strict=True), 1)
        ]
        payload = bytes((session, session)) + b"".join(descriptors)
        return len(payload).to_bytes(2, "big") + payload

    return build
