export type Pin = { id: string; x: number; y: number; note: string };

export function PinOverlay(props: {
  src: string;
  pins: Pin[];
  editable: boolean;
  onAddPin?: (p: { x: number; y: number }) => void;
}) {
  const { src, pins, editable, onAddPin } = props;
  function handleClick(e: React.MouseEvent<HTMLDivElement>) {
    if (!editable || !onAddPin) return;
    const r = e.currentTarget.getBoundingClientRect();
    onAddPin({ x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
  }
  return (
    <div
      data-testid="pin-surface"
      onClick={handleClick}
      className="relative inline-block rounded-card border border-line"
      style={{ cursor: editable ? "crosshair" : "default" }}
    >
      <img src={src} alt="" className="block max-w-full rounded-card" />
      {pins.map((p, i) => (
        <span
          key={p.id}
          data-testid={`pin-${p.id}`}
          className="absolute flex h-5 w-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full bg-live text-xs text-surface-0"
          style={{ left: `${p.x * 100}%`, top: `${p.y * 100}%` }}
          title={p.note}
        >
          {i + 1}
        </span>
      ))}
    </div>
  );
}
