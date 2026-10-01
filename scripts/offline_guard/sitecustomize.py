"""Inherited by Python subprocesses during the hermetic offline suite."""
import socket


def denied(*args, **kwargs):
    raise AssertionError("External sockets disabled in offline test process")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
