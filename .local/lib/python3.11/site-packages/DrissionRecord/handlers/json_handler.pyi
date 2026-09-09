# -*- coding:utf-8 -*-
from .base_handler import TextLikeHandler
from ..tools import no_left_right, is_single_data


class JSONHandler(TextLikeHandler):

    @property
    def _add_lr_method(self):
        return self._recorder._settings.get('add_lr_method', no_left_right)

    def _handle_data(self, data):
        return data if is_single_data(data) else self._add_lr_method(self, data)


def _parse_coord(coord):
    if isinstance(coord, (list, tuple)):
        return coord
    elif isinstance(coord, (int, str)):
        return [coord]
    elif coord is None:
        return [0]
    else:
        raise TypeError(f'coord参数只能是int、str、list或tuple，现在是{coord}')


def nav(data, parts):
    current = data
    parent = None
    last_key = None
    for i, part in enumerate(parts):
        parent = current
        last_key = part
        if isinstance(part, int) and part > 0:
            part -= 1
        if i == len(parts) - 1:
            break

        if isinstance(current, dict):
            if not isinstance(part, str):
                raise TypeError(f"字典中的索引需使用str，而非 {part}")

            if part not in current:
                current[part] = {} if isinstance(parts[i + 1], str) else []

            current = current[part]

        elif isinstance(current, list):
            if isinstance(part, int):
                if len(current) < part:
                    while len(current) < part:
                        current.append(None)
                    current[-1] = {} if isinstance(parts[i + 1], str) else []
                current = current[part]
            else:
                raise TypeError(f"列表索引必须是int，而非 {part}")

        else:
            raise TypeError(f"无法导航至 {current}")

    return parent, last_key
