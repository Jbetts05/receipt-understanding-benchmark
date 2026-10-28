import { useEffect, useState, type CSSProperties } from "react";

import type { ReceiptSample } from "../types";
import { FileIcon } from "./Icons";

export function sameOriginImageUrl(
  value: string | null,
  origin = typeof window === "undefined" ? "http://localhost" : window.location.origin,
): string | null {
  if (value === null || value.trim() === "") {
    return null;
  }
  try {
    const resolved = new URL(value, origin);
    if (resolved.origin !== origin) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

export function ReceiptPreview({
  sample,
  selectedPath = null,
  compact = false,
}: {
  sample: ReceiptSample;
  selectedPath?: string | null;
  compact?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const [fit, setFit] = useState(true);
  const [zoom, setZoom] = useState(1);
  const imageUrl = sameOriginImageUrl(sample.image_url);

  useEffect(() => {
    setFailed(false);
    setFit(true);
    setZoom(1);
  }, [imageUrl, sample.id]);

  function adjustZoom(delta: number) {
    setFit(false);
    setZoom((current) => Math.min(3, Math.max(0.5, current + delta)));
  }

  const unavailable = imageUrl === null || !sample.image_available || failed;
  return (
    <figure
      className={`receipt-preview${compact ? " receipt-preview-compact" : ""}`}
      aria-label={`${sample.title} source image`}
    >
      <div
        className={`receipt-image-viewport${fit ? " is-fit" : ""}`}
        data-active-field={selectedPath ?? undefined}
      >
        {unavailable ? (
          <div className="receipt-image-failure" role="status">
            <FileIcon />
            <strong>Source image unavailable</strong>
            {!compact && <span>{sample.filename}</span>}
          </div>
        ) : (
          <img
            src={imageUrl}
            alt={`${sample.title} receipt`}
            loading={compact ? "lazy" : "eager"}
            draggable={false}
            onError={() => setFailed(true)}
            style={{ "--receipt-scale": zoom } as CSSProperties}
          />
        )}
      </div>
      {!compact && !unavailable && (
        <figcaption className="receipt-controls" aria-label="Receipt image controls">
          <button
            type="button"
            onClick={() => adjustZoom(-0.25)}
            aria-label="Zoom out"
          >
            −
          </button>
          <output aria-live="polite">{Math.round(zoom * 100)}%</output>
          <button
            type="button"
            onClick={() => adjustZoom(0.25)}
            aria-label="Zoom in"
          >
            +
          </button>
          <button
            type="button"
            className={fit ? "active" : ""}
            aria-pressed={fit}
            onClick={() => {
              setFit(true);
              setZoom(1);
            }}
          >
            Fit
          </button>
          <button
            type="button"
            onClick={() => {
              setFit(true);
              setZoom(1);
            }}
          >
            Reset
          </button>
        </figcaption>
      )}
    </figure>
  );
}
