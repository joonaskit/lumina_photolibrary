"""One Lumina per user: a second `lumina PATH` hands its paths to the running window
and ends, instead of opening the same database a second time.

A local socket (a Unix socket in the runtime directory) named for the user; the message is
the paths as JSON on one line.
"""

import json
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from lumina import APP_ID

# How long to wait for a running instance, in milliseconds
_TIMEOUT_MS = 1000


def socket_name():
    return f"{APP_ID}-{os.getuid()}" if hasattr(os, "getuid") else APP_ID


class SingleInstance(QObject):
    # Paths another start asked this one to open; empty for "show the window"
    opened = Signal(list)

    def __init__(self, name=None, parent=None):
        super().__init__(parent)
        self.name = name or socket_name()
        self._server = None
        self._clients = {}

    def forward(self, paths):
        """Hand `paths` to the running instance. True when there is one and it has
        them; False when this is the first (or the other does not answer)."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(_TIMEOUT_MS):
            return False
        socket.write(json.dumps(list(paths)).encode() + b"\n")
        sent = socket.waitForBytesWritten(_TIMEOUT_MS)
        socket.disconnectFromServer()
        if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            socket.waitForDisconnected(_TIMEOUT_MS)
        return sent

    def listen(self):
        """Become the instance others forward to. False when that failed (the app still
        runs; a second start is then a second window)."""
        self._server = QLocalServer(self)
        # Only this user's processes
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        # A crash leaves the socket file behind: nobody answered in forward(), so it is stale
        QLocalServer.removeServer(self.name)
        return self._server.listen(self.name)

    def close(self):
        if self._server is not None:
            self._server.close()

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            # Held here until the other end hangs up, with what it has sent so far
            self._clients[socket] = bytearray()
            socket.readyRead.connect(self._read)
            socket.disconnected.connect(self._hung_up)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket=None):
        socket = socket or self.sender()
        buffer = self._clients.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if b"\n" in buffer:
            self._deliver(bytes(buffer))
            buffer.clear()

    def _hung_up(self):
        socket = self.sender()
        self._read(socket)
        if self._clients.pop(socket, None) is not None:
            socket.deleteLater()

    def _deliver(self, data):
        try:
            paths = json.loads(data.split(b"\n", 1)[0])
        except ValueError:
            return
        if isinstance(paths, list):
            self.opened.emit([path for path in paths if isinstance(path, str)])
