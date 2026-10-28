import { useState } from "react";
import { Icon } from "./icons";

/** Model preview image; falls back to a neutral cube icon when no thumbnail exists yet. */
export function ModelThumb({
  id,
  hasThumbnail,
  size = 56,
  alt = "",
}: {
  id: string;
  hasThumbnail: boolean;
  size?: number;
  alt?: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!hasThumbnail || failed) {
    return (
      <span className="model-thumb placeholder" style={{ width: size, height: size }} aria-hidden={!alt} title={alt}>
        <Icon name="cube" size={Math.round(size * 0.45)} />
      </span>
    );
  }
  return (
    <img
      className="model-thumb"
      src={`/api/library/${id}/thumbnail`}
      width={size}
      height={size}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
