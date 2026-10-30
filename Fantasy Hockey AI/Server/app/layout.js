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

// "2026-10-31" as a local date (a date, not a moment: no time zone shift).
export function dateOf(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d);
}

// A day picker's name for a day: Today, Tomorrow, else the day of the week.
export function dayName(entry, week) {
  if (entry.state === 'today') return 'Today';
  const today = week.find((d) => d.state === 'today');
  if (today && dateOf(entry.date) - dateOf(today.date) === 86400000) return 'Tomorrow';
  return dateOf(entry.date).toLocaleDateString('en-US', { weekday: 'long' });
}

export const shortDay = (iso) => dateOf(iso).toLocaleDateString('en-US', { weekday: 'short' });

// The fantasy week's day picker (Roster, Games): `week` [{date, state: final|today|ahead}], `day`
// 'today' or a date. The button shows the day; when not `active` (Roster's Season), its label
// calls `onActivate` and only the arrow opens the list. Each day shows Final / Live / To play and
// `right(d)` -> [text, class]; `unavailable(d)`: why it can't be picked, or null.
export function DayPicker({ week, day, onDay, right, active = true, onActivate, unavailable = () => null }) {
  const [open, setOpen] = useState(false);
  useEscape(open, () => setOpen(false));
  const current = week.find((d) => (day === 'today' ? d.state === 'today' : d.date === day)) || week.find((d) => d.state === 'today');
  return html`<div class="day-pick">
    <div class=${`day-btn ${active ? 'on' : ''}`}>
      <button type="button" aria-pressed=${active}
        onClick=${() => (active ? setOpen(!open) : onActivate())}>${current ? dayName(current, week) : 'Today'}</button>
      <button type="button" class="arrow" aria-haspopup="listbox" aria-expanded=${open} aria-label="Choose a day"
        onClick=${() => setOpen(!open)}><svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"></path></svg></button>
    </div>
    ${open && html`<div class="day-scrim" aria-hidden="true" onClick=${() => setOpen(false)}></div>
      <div class="day-list" role="listbox" aria-label="Day">
        ${week.map((d) => {
          const on = active && current && d.date === current.date;
          const why = unavailable(d);
          const [text, cls] = right(d);
          return html`<button type="button" role="option" aria-selected=${on} class=${on ? 'on' : ''}
            disabled=${!!why} title=${why || undefined}
            onClick=${() => { setOpen(false); onDay(d.state === 'today' ? 'today' : d.date); }}>
            <span class="what"><span class="nm">${dayName(d, week)}</span>
              <span class=${`st ${d.state === 'today' ? 'you' : ''}`}>${d.state === 'final' ? 'Final' : d.state === 'today' ? 'Live' : 'To play'}</span></span>
            <span class=${`pts ${cls || ''}`}>${text}</span>
          </button>`;
        })}
      </div>`}
  </div>`;
}
