// Shared by the standalone viewer and the app: mounts a Photo Sphere Viewer virtual tour for a manifest.
import { Viewer } from '@photo-sphere-viewer/core';
import { MarkersPlugin } from '@photo-sphere-viewer/markers-plugin';
import { VirtualTourPlugin } from '@photo-sphere-viewer/virtual-tour-plugin';

export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/**
 * @param container   element to render into
 * @param manifest    the room manifest
 * @param manifestUrl where panorama paths are resolved from
 * @param available   optional Set of room ids that have a panorama; others are left out (partial renders)
 * @param onRoomChange called with the room id whenever the visitor moves to another room
 */
export function mountTour({ container, manifest, manifestUrl, available = null, onRoomChange = () => {} }) {
  const meta = manifest.metadata;
  const money = (n) => `${esc(meta.currency)} ${Math.round(n).toLocaleString()}`;
  const nodes = manifest.topology.nodes.filter((n) => !available || available.has(n.room_id));
  if (!nodes.length) throw new Error('no rendered rooms to show yet');
  const ids = new Set(nodes.map((n) => n.room_id));

  const tourNodes = nodes.map((n) => ({
    id: n.room_id,
    name: n.room_name,
    panorama: new URL(n.asset_url, manifestUrl).href,
    links: n.hotspots.filter((h) => h.type === 'teleport' && ids.has(h.payload.target_room_id)).map((h) => ({
      nodeId: h.payload.target_room_id,
      position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch },
      name: h.payload.label,
    })),
    markers: n.hotspots.filter((h) => h.type === 'product_tag').map((h) => ({
      id: h.id,
      position: { yaw: h.coordinates.yaw, pitch: h.coordinates.pitch },
      html: `<div class="tag">${esc(h.payload.title)} · ${money(h.payload.cost)}</div>`,
      anchor: 'bottom center',
      data: h.payload,
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
    setRoom: (id) => tour.setCurrentNode(id),
    gotoMarker: (id) => markers.gotoMarker(id, 800),
    destroy: () => viewer.destroy(),
  };
}
