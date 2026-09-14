"""The montage service as a service: its door, and the values that go through it.

Everything else under `montage/` is a library — pure enough that AUT calls it
in the same process, which is how it runs today and how §13.3 said to build it:
package first, network second. This subpackage is the second half, and it is
deliberately the only place in the service allowed to know what HTTP is.

Nothing here decides anything about a montage. It reads values off the wire,
hands them to the same functions AUT would have called directly, and writes
the answers back — so the two ways of calling cannot drift apart, because
there is only one implementation underneath both.
"""
from __future__ import annotations

from montage.service.wire import (
    clip_request_from_dict,
    clip_request_to_dict,
    library_from_dict,
    library_to_dict,
    render_result_from_dict,
    render_result_to_dict,
)

__all__ = [
    "clip_request_from_dict",
    "clip_request_to_dict",
    "library_from_dict",
    "library_to_dict",
    "render_result_from_dict",
    "render_result_to_dict",
]
