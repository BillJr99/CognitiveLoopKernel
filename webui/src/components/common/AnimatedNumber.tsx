import { useEffect, useRef, useState } from "react";

// Smoothly counts from the previous value to the next, and briefly flashes
// when it changes — gives the dashboard a lively, "alive" feel.
export function AnimatedNumber({
  value,
  format,
  className = "",
}: {
  value: number;
  format?: (n: number) => string;
  className?: string;
}) {
  const [display, setDisplay] = useState(value);
  const [flash, setFlash] = useState(false);
  const fromRef = useRef(value);
  const rafRef = useRef<number>();

  useEffect(() => {
    const from = fromRef.current;
    const to = value;
    if (from === to) return;
    setFlash(true);
    const start = performance.now();
    const dur = 600;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplay(from + (to - from) * eased);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = to;
        setDisplay(to);
        setTimeout(() => setFlash(false), 250);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [value]);

  const text = format ? format(display) : Math.round(display).toString();
  return <span className={`${className} ${flash ? "flash" : ""}`}>{text}</span>;
}
