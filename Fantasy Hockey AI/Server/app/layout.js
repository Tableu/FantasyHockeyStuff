// The PC layout (plan: the Fantasy Hockey PC design canvas): a window at least WIDE_PX wide gets
// the sidebar, wide tables and side panels instead of sheets; narrower ones keep the phone layout.
import { html, useState, useEffect, useRef } from './vendor/htm-preact-3.1.1.mjs';

export const WIDE_PX = 1024;

// A refresh asked for from inside a screen (NoPlan's button); App runs it like a tap in More.
export const REFRESH_EVENT = 'fh:refresh';
export function askRefresh(mode = 'quick') {
  dispatchEvent(new CustomEvent(REFRESH_EVENT, { detail: { mode } }));
}

// A screen's empty state before the day's first plan: the header has no refresh buttons.
export function NoPlan() {
  return html`<div class="empty">No plan saved for today yet.
    <br /><button type="button" class="btn primary small" style="margin-top: 10px" onClick=${() => askRefresh('quick')}>Make a plan</button>
    <span class="dim pull-hint"> or pull down</span></div>`;
}
const QUERY = `(min-width: ${WIDE_PX}px)`;

// Whether the window is wide enough for the PC layout, following it as the window is resized.
export function useWide() {
  const media = typeof matchMedia === 'function' ? matchMedia(QUERY) : null;
  const [wide, setWide] = useState(() => !!media && media.matches);
  useEffect(() => {
    if (!media) return undefined;
    const on = () => setWide(media.matches);
    media.addEventListener('change', on);
    on();
    return () => media.removeEventListener('change', on);
  }, []);
  return wide;
}

// Esc runs `onEscape` while `active` (a panel or menu that is open).
export function useEscape(active, onEscape) {
  const latest = useRef(onEscape);
  latest.current = onEscape;
  useEffect(() => {
    if (!active) return undefined;
    const on = (e) => { if (e.key === 'Escape') latest.current(); };
    document.addEventListener('keydown', on);
    return () => document.removeEventListener('keydown', on);
  }, [active]);
}

// A player's headshot (the NHL's, assets.nhle.com: the server sends its URL), a circle; his
// initials in the circle when there is none or it cannot load (one just traded may have none
// under his new team yet).
export function Photo({ src, name, size = 26 }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  const style = `width: ${size}px; height: ${size}px`;
  if (!src || failed) {
    const initials = (name || '').replace(/\(.*\)/, '').split(/[\s.]+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join('');
    return html`<span class="photo none" style=${`${style}; font-size: ${Math.round(size * 0.38)}px`} aria-hidden="true">${initials}</span>`;
  }
  return html`<span class="photo" style=${style}><img src=${src} alt="" loading="lazy" decoding="async" onError=${() => setFailed(true)} /></span>`;
}
