"""Configured machine identity, shared with Python 3.8 host-side helpers."""
import os
import platform
import socket


def host_os(host):
    value = host.get("os", "").lower()
    return "darwin" if value == "macos" else value


def configured_host(cfg, value, source):
    if value not in cfg["hosts"]:
        raise ValueError("{}: unknown host {!r}; choose from {}".format(
            source, value, ", ".join(cfg["hosts"])))
    return value


def detect_host(cfg, system=None):
    hosts = cfg["hosts"]
    if "ROUTE_HOST" in os.environ:
        return configured_host(cfg, os.environ["ROUTE_HOST"], "ROUTE_HOST")
    hostname = socket.gethostname().split(".", 1)[0]
    matches = [h for h, info in hosts.items()
               if info.get("match", {}).get("hostname") == hostname]
    if not matches:
        system = (system or platform.system()).lower()
        matches = [h for h, info in hosts.items() if host_os(info) == system]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("cannot identify this machine ({}, {}); {}. "
                     "set ROUTE_HOST or hosts.<h>.match.hostname; configured hosts: {}".format(
                         hostname, platform.system().lower(),
                         "ambiguous hosts: " + ", ".join(matches) if matches else "no matching host",
                         ", ".join(hosts)))


def default_linux_host(cfg):
    hosts = cfg["hosts"]
    if "default_linux_host" in cfg:
        return configured_host(cfg, cfg["default_linux_host"], "config: default_linux_host")
    linux = [h for h, info in hosts.items() if host_os(info) == "linux"]
    if len(linux) > 1:
        raise ValueError("config: default_linux_host is required when Linux hosts can tie: " + ", ".join(linux))
    return next(iter(linux), None)
