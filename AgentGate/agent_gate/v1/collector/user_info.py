"""User information collector: UID, GID, username, groups.

跨平台说明：``pwd`` / ``grp`` 是 POSIX 专有模块，``os.getuid()`` 在 Windows
上也不存在 —— 三者都做可选降级。Windows 上 uid/gid 取 **-1**（与 TS 端一致：
Node 的 ``os.userInfo()`` 在 Windows 同样返回 -1），用户名用 ``getpass``
兜底为登录名。早期版本在模块顶层 ``import pwd``，导致 Windows 上
``v1.collector`` 一导入就 ImportError，进而 analyze 接口整体不可用。
"""

import os


class UserInfoCollector:
    """Collects current user identity information."""

    @staticmethod
    def collect() -> dict:
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        # 无 getuid（Windows）时取 -1，与 TS 端 os.userInfo() 的平台行为对齐
        uid = getuid() if getuid else -1
        gid = getgid() if getgid else -1
        username = ""
        groups: list[str] = []

        try:
            import pwd
            if uid != -1:
                username = pwd.getpwuid(uid).pw_name
        except Exception:
            pass

        if not username:
            # Windows（无 pwd 模块）兜底：取登录用户名
            try:
                import getpass
                username = getpass.getuser()
            except Exception:
                pass

        try:
            import grp
            if gid is not None:
                groups.append(grp.getgrgid(gid).gr_name)
        except Exception:
            pass

        try:
            # Get all groups for current user
            import grp
            for gr_entry in grp.getgrall():
                if username and username in gr_entry.gr_mem:
                    groups.append(gr_entry.gr_name)
        except Exception:
            pass

        return {
            "user": {
                "uid": uid,
                "gid": gid,
                "username": username,
                "groups": groups,
            }
        }
