"""用户确认的通道组合与存储深度规则；未列出的组合不推断。"""


def expected_depth_m(channels):
    combination = frozenset(channels)
    if combination in (frozenset((1, 3)), frozenset((2, 4))):
        return 512
    if combination in (frozenset((1, 2)), frozenset((1, 2, 3, 4)),
                       frozenset((2, 3, 4)), frozenset((3, 4))):
        return 256
    return None
