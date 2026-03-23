'use strict';

const { WebSocketServer, OPEN } = require('ws');

const PORT = process.env.PORT || 8765;

// rooms: Map<roomId: string, Set<WebSocket>>
const rooms = new Map();

const wss = new WebSocketServer({ port: PORT });

wss.on('connection', (ws) => {
  ws.roomId = null;

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      console.warn('Received non-JSON message, ignoring.');
      return;
    }

    const { type, room } = msg;

    if (type === 'join') {
      if (!room) { console.warn('join message missing room field'); return; }
      ws.roomId = room;
      if (!rooms.has(room)) rooms.set(room, new Set());
      rooms.get(room).add(ws);
      const count = rooms.get(room).size;
      console.log(`[${room}] peer joined (${count} in room)`);
      return;
    }

    if (['offer', 'answer', 'call', 'accept', 'reject'].includes(type)) {
      if (!ws.roomId) { console.warn(`Peer sent '${type}' before joining a room`); return; }
      const peers = rooms.get(ws.roomId);
      if (!peers) return;
      let forwarded = 0;
      for (const peer of peers) {
        if (peer !== ws && peer.readyState === OPEN) {
          peer.send(raw); // forward raw string — never re-serialize SDP
          forwarded++;
        }
      }
      console.log(`[${ws.roomId}] relayed ${type} to ${forwarded} peer(s)`);
      return;
    }

    console.warn(`Unknown message type: ${type}`);
  });

  ws.on('close', () => {
    if (!ws.roomId) return;
    const peers = rooms.get(ws.roomId);
    if (peers) {
      peers.delete(ws);
      if (peers.size === 0) rooms.delete(ws.roomId);
    }
    console.log(`[${ws.roomId}] peer disconnected`);
  });

  ws.on('error', (err) => {
    console.error('WebSocket error:', err.message);
  });
});

console.log(`Signaling server listening on port ${PORT}`);
