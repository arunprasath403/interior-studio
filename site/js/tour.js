// Mounts the 360° walkthrough for a design. Images come from the server, one web address per finished room.
import { Viewer } from '@photo-sphere-viewer/core';
import { MarkersPlugin } from '@photo-sphere-viewer/markers-plugin';
import { VirtualTourPlugin } from '@photo-sphere-viewer/virtual-tour-plugin';
import { esc, money, roomTitle } from './format.js';

export function mountTour({ container, manifest, images, onRoomChange = () => {} }) {
  const currency = manifest.metadata.currency;
  const nodes = manifest.topology.nodes.filter((n) => images[n.room_id]);
  if (!nodes.length) throw new Error('no rooms are ready to explore yet');
  const ids = new Set(nodes.map((n) => n.room_id));

  const tourNodes = nodes.map((n) => ({
    id: n.room_id,
    name: roomTitle(n),
    panorama: images[n.room_id],
    links: n.hotspots.filter((h) => h.type === 'teleport' && ids.has(h.payload.target_room_id)).map((h) => ({
      nodeId: h.payload.target_room_id,
      position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch },
      name: h.payload.label,
    })),
    markers: n.hotspots.filter((h) => h.type === 'product_tag').map((h) => ({
      id: h.id,
      position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch },
      html: `<div class="tag">${esc(h.payload.title)} · ${esc(money(h.payload.cost, currency))}</div>`,
      anchor: 'bottom center',
    })),
  }));

  const viewer = new Viewer({
    container,
    navbar: ['zoom', 'move', 'fullscreen'],
    plugins: [MarkersPlugin, [VirtualTourPlugin, { positionMode: 'manual', renderMode: '2d', preload: false }]],
  });
  const tour = viewer.getPlugin(VirtualTourPlugin);
  const markers = viewer.getPlugin(MarkersPlugin);
  tour.addEventListener('node-changed', ({ node }) => onRoomChange(node.id));
  tour.setNodes(tourNodes, tourNodes[0].id);
  onRoomChange(tourNodes[0].id);

  return {
    rooms: nodes,
    goToRoom: (id) => tour.setCurrentNode(id),
    goToItem: (id) => markers.gotoMarker(id, 800),
    destroy: () => viewer.destroy(),
  };
}
