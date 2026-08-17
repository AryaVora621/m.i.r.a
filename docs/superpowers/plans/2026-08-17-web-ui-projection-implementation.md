# M.I.R.A. Web Ambient Projection UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scaffold and build the M.I.R.A. Ambient Web Projection UI (`/web`) in TypeScript React + Vite, customized for a compact $30'' \times 40''$ Raspberry Pi Wall Projector surface with `#000000` true-black canvas, Electric Purple & Neon Orange accents, 3D Wireframe Globe, `Zen Dots` / `Geo` fonts, uniform micro-pill corner widgets, 60 FPS spatial cursor, and local WebSocket test harness.

**Architecture:** A lightweight React 19 SPA powered by Vite and vanilla CSS custom properties. Component hierarchy: `WallCanvas` root -> `WireframeGlobe` center -> `WidgetClock` micro-pill top-left -> `WidgetNodeStatus` micro-pill top-right -> `WidgetWeather` micro-pill bottom-left -> `WidgetMusic` micro-pill bottom-right -> `SpatialCursor` overlay. Connected to native WebSockets (`ws://localhost:8080/ws`).

**Tech Stack:** React 19, TypeScript, Vite, CSS Custom Properties (`index.css`), Google Fonts (`Zen Dots` + `Geo`), Node.js `ws` library for mock server.

## Global Constraints

- **Canvas Background**: `#000000` pitch black (0% light bleed for room wall projection).
- **Primary Colors**: Electric Purple (`#9333ea` / `#c084fc`), Vivid Neon Orange (`#ff6b00` / `#ff9e00`).
- **Typography**: Display titles = `Zen Dots`, Telemetry/Subtext = `Geo`.
- **Target Wall Dimensions**: Compact $30'' \text{ wide} \times 40'' \text{ high}$ vertical wall scale (no voice text popups).
- **Cursor Performance**: Hardware-accelerated 2D ring reticle using CSS `translate3d(x, y, 0)` at 60 FPS.

---

### Task 1: Scaffold React 19 + Vite Application & Configure Design Tokens

**Files:**
- Create: `web/package.json`
- Create: `web/vite.config.ts`
- Create: `web/index.html`
- Create: `web/src/index.css`
- Create: `web/src/main.tsx`
- Create: `web/src/types.ts`

**Interfaces:**
- Consumes: None (Root scaffolding)
- Produces: `types.ts` (`GestureEvent`, `SpeechEvent`, `MediaEvent`, `TelemetryEvent` interfaces)

- [ ] **Step 1: Write `package.json` for `web/`**

```json
{
  "name": "mira-web",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview",
    "mock-server": "node mock-server.cjs"
  },
  "dependencies": {
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  },
  "devDependencies": {
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "@vitejs/plugin-react": "^4.3.0",
    "typescript": "^5.6.0",
    "vite": "^6.0.0",
    "ws": "^8.18.0"
  }
}
```

- [ ] **Step 2: Create `vite.config.ts`**

```typescript
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true
  }
});
```

- [ ] **Step 3: Create `index.html` with Google Fonts (`Zen Dots` & `Geo`)**

```html
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>M.I.R.A. Ambient Projection UI</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Geo:ital@0;1&family=Zen+Dots&display=swap" rel="stylesheet" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 4: Create `src/index.css` with Design System Tokens**

```css
:root {
  --bg-black: #000000;
  --purple-primary: #9333ea;
  --purple-glow: #c084fc;
  --purple-subtle: rgba(147, 51, 234, 0.15);
  --orange-primary: #ff6b00;
  --orange-glow: #ff9e00;
  --orange-subtle: rgba(255, 107, 0, 0.15);
  --text-main: #f8fafc;
  --text-muted: #94a3b8;
  --font-display: 'Zen Dots', cursive, sans-serif;
  --font-geo: 'Geo', monospace;
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  background-color: var(--bg-black);
  color: var(--text-main);
  font-family: var(--font-geo);
  overflow: hidden;
  width: 100vw;
  height: 100vh;
}

.glass-card {
  background: rgba(8, 6, 16, 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(192, 132, 252, 0.25);
  border-radius: 10px;
}
```

- [ ] **Step 5: Create `src/types.ts` for Protocol Events**

```typescript
export interface GestureEvent {
  node_id: string;
  timestamp: number;
  x: number;
  y: number;
  gesture_type: 'HOVER' | 'PINCH' | 'SWIPE_LEFT' | 'SWIPE_RIGHT';
}

export interface SpeechEvent {
  state: 'IDLE' | 'LISTENING' | 'THINKING' | 'SPEAKING';
  transcript: string;
}

export interface MediaEvent {
  track: string;
  artist: string;
  album: string;
  playing: boolean;
  progress_percent: number;
}

export interface TelemetryEvent {
  node_id: string;
  fps: number;
  status: string;
}
```

- [ ] **Step 6: Create `src/main.tsx` Root**

```typescript
import React from 'react';
import ReactDOM from 'react me-dom/client';
import App from './App';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 7: Commit Task 1**

```bash
git add web/
git commit -m "feat(web): scaffold React 19 + Vite app and index.css design system"
```

---

### Task 2: Build `WebSocketProvider` Context & 60 FPS `SpatialCursor` Component

**Files:**
- Create: `web/src/context/WebSocketContext.tsx`
- Create: `web/src/components/SpatialCursor.tsx`
- Test: Build simple unit test or mock event verification

**Interfaces:**
- Consumes: `types.ts` (`GestureEvent`, `SpeechEvent`, `MediaEvent`, `TelemetryEvent`)
- Produces: `useWebSocket()` hook providing live cursor coordinates $(x, y)$ and event states

- [ ] **Step 1: Create `src/context/WebSocketContext.tsx`**

```typescript
import React, { createContext, useContext, useEffect, useState } from 'react';
import { GestureEvent, SpeechEvent, MediaEvent, TelemetryEvent } from '../types';

interface WebSocketContextType {
  connected: boolean;
  gesture: GestureEvent | null;
  speech: SpeechEvent;
  media: MediaEvent;
  telemetry: TelemetryEvent;
}

const defaultSpeech: SpeechEvent = { state: 'IDLE', transcript: '' };
const defaultMedia: MediaEvent = { track: 'STARBOY', artist: 'The Weeknd', album: 'Starboy', playing: true, progress_percent: 45 };
const defaultTelemetry: TelemetryEvent = { node_id: 'pi-projector', fps: 60, status: 'ONLINE' };

const WebSocketContext = createContext<WebSocketContextType>({
  connected: false,
  gesture: null,
  speech: defaultSpeech,
  media: defaultMedia,
  telemetry: defaultTelemetry
});

export const WebSocketProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [connected, setConnected] = useState(false);
  const [gesture, setGesture] = useState<GestureEvent | null>(null);
  const [speech, setSpeech] = useState<SpeechEvent>(defaultSpeech);
  const [media, setMedia] = useState<MediaEvent>(defaultMedia);
  const [telemetry, setTelemetry] = useState<TelemetryEvent>(defaultTelemetry);

  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8080/ws');

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'GESTURE_EVENT') setGesture(data.payload);
        if (data.type === 'SPEECH_EVENT') setSpeech(data.payload);
        if (data.type === 'MEDIA_EVENT') setMedia(data.payload);
        if (data.type === 'TELEMETRY_EVENT') setTelemetry(data.payload);
      } catch (err) {
        console.error('WS Parse Error', err);
      }
    };

    return () => ws.close();
  }, []);

  return (
    <WebSocketContext.Provider value={{ connected, gesture, speech, media, telemetry }}>
      {children}
    </WebSocketContext.Provider>
  );
};

export const useWebSocket = () => useContext(WebSocketContext);
```

- [ ] **Step 2: Create `src/components/SpatialCursor.tsx`**

```typescript
import React from 'react';
import { useWebSocket } from '../context/WebSocketContext';

export const SpatialCursor: React.FC = () => {
  const { gesture } = useWebSocket();

  if (!gesture) return null;

  const leftPercent = gesture.x * 100;
  const topPercent = gesture.y * 100;

  return (
    <div
      style={{
        position: 'absolute',
        left: `${leftPercent}%`,
        top: `${topPercent}%`,
        transform: 'translate(-50%, -50%)',
        pointerEvents: 'none',
        zIndex: 9999,
        transition: 'transform 0.016s linear'
      }}
    >
      <div
        style={{
          width: '28px',
          height: '28px',
          borderRadius: '50%',
          border: '2px solid #ff9e00',
          backgroundColor: gesture.gesture_type === 'PINCH' ? 'rgba(255, 107, 0, 0.6)' : 'rgba(255, 107, 0, 0.12)',
          boxShadow: '0 0 15px #ff6b00',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center'
        }}
      >
        <div style={{ width: '5px', height: '5px', borderRadius: '50%', backgroundColor: '#ffffff' }} />
      </div>
    </div>
  );
};
```

- [ ] **Step 3: Commit Task 2**

```bash
git add web/src/context/ web/src/components/SpatialCursor.tsx
git commit -m "feat(web): add WebSocketContext and 60FPS SpatialCursor component"
```

---

### Task 3: Build `WireframeGlobe` 3D Purple Vector Component

**Files:**
- Create: `web/src/components/WireframeGlobe.tsx`

**Interfaces:**
- Consumes: None (React component)
- Produces: `WireframeGlobe` central vector sphere

- [ ] **Step 1: Create `src/components/WireframeGlobe.tsx`**

```typescript
import React from 'react';

export const WireframeGlobe: React.FC = () => {
  return (
    <div
      style={{
        position: 'absolute',
        top: '45%',
        left: '50%',
        transform: 'translate(-50%, -50%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center'
      }}
    >
      <div style={{ width: '130px', height: '130px', position: 'relative', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        {/* Outer dashed ring */}
        <div
          style={{
            position: 'absolute',
            inset: '-10px',
            borderRadius: '50%',
            border: '1px dashed rgba(192, 132, 252, 0.35)',
            animation: 'spin 30s linear infinite'
          }}
        />

        {/* 3D Wireframe Globe SVG */}
        <svg
          width="120"
          height="120"
          viewBox="0 0 100 100"
          fill="none"
          style={{
            filter: 'drop-shadow(0 0 14px rgba(168, 85, 247, 0.8)) drop-shadow(0 0 8px rgba(255, 107, 0, 0.4))'
          }}
        >
          <circle cx="50" cy="50" r="46" stroke="#c084fc" strokeWidth="1.8" />
          <ellipse cx="50" cy="50" rx="46" ry="14" stroke="#a855f7" strokeWidth="1.2" strokeDasharray="4 2" />
          <ellipse cx="50" cy="50" rx="46" ry="28" stroke="#c084fc" strokeWidth="1.2" />
          <line x1="50" y1="4" x2="50" y2="96" stroke="#ff9e00" strokeWidth="1.5" />
          <line x1="4" y1="50" x2="96" y2="50" stroke="#ff9e00" strokeWidth="1.5" />
          <circle cx="50" cy="4" r="2.5" fill="#ff6b00" />
          <circle cx="50" cy="96" r="2.5" fill="#ff6b00" />
          <circle cx="4" cy="50" r="2.5" fill="#ff6b00" />
          <circle cx="96" cy="50" r="2.5" fill="#ff6b00" />
          <circle cx="50" cy="50" r="3" fill="#ffffff" />
        </svg>
      </div>
    </div>
  );
};
```

- [ ] **Step 2: Commit Task 3**

```bash
git add web/src/components/WireframeGlobe.tsx
git commit -m "feat(web): add WireframeGlobe 3D vector component"
```

---

### Task 4: Build `WidgetClock` & `WidgetNodeStatus` Compact Components

**Files:**
- Create: `web/src/components/WidgetClock.tsx`
- Create: `web/src/components/WidgetNodeStatus.tsx`

- [ ] **Step 1: Create `src/components/WidgetClock.tsx`**

```typescript
import React, { useState, useEffect } from 'react';

export const WidgetClock: React.FC = () => {
  const [timeStr, setTimeStr] = useState('');
  const [dateStr, setDateStr] = useState('');

  useEffect(() => {
    const update = () => {
      const now = new Date();
      setTimeStr(now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }));
      setDateStr(now.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }));
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="glass-card" style={{ position: 'absolute', top: '18px', left: '18px', padding: '10px 14px', display: 'flex', alignItems: 'center', gap: '12px' }}>
      <div>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: '20px', letterSpacing: '1px', color: '#ffffff', textShadow: '0 0 10px var(--purple-glow)', lineHeight: 1 }}>
          {timeStr || '21:20'}
        </div>
        <div style={{ fontFamily: 'var(--font-geo)', fontSize: '13px', color: 'var(--orange-glow)', textTransform: 'uppercase', marginTop: '2px' }}>
          {dateStr || 'Mon, Aug 17'}
        </div>
      </div>
      <div style={{ borderLeft: '1px solid rgba(255, 255, 255, 0.15)', paddingLeft: '12px' }}>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: '13px', color: 'var(--text-main)' }}>72°F</div>
        <div style={{ fontFamily: 'var(--font-geo)', fontSize: '12px', color: 'var(--text-muted)' }}>Clear</div>
      </div>
    </div>
  );
};
```

- [ ] **Step 2: Create `src/components/WidgetNodeStatus.tsx`**

```typescript
import React from 'react';
import { useWebSocket } from '../context/WebSocketContext';

export const WidgetNodeStatus: React.FC = () => {
  const { connected, telemetry } = useWebSocket();

  return (
    <div className="glass-card" style={{ position: 'absolute', top: '18px', right: '18px', padding: '6px 12px', display: 'flex', alignItems: 'center', gap: '8px', fontFamily: 'var(--font-geo)', fontSize: '13px', color: 'var(--orange-glow)' }}>
      <span
        style={{
          width: '6px',
          height: '6px',
          borderRadius: '50%',
          backgroundColor: connected ? 'var(--orange-primary)' : '#ef4444',
          boxShadow: connected ? '0 0 8px var(--orange-primary)' : 'none'
        }}
      />
      {telemetry.status} • {telemetry.fps} FPS
    </div>
  );
};
```

- [ ] **Step 3: Commit Task 4**

```bash
git add web/src/components/WidgetClock.tsx web/src/components/WidgetNodeStatus.tsx
git commit -m "feat(web): add compact WidgetClock and WidgetNodeStatus components"
```

---

### Task 5: Build `WidgetMusic` Compact Audio Player Component & Assemble `App.tsx`

**Files:**
- Create: `web/src/components/WidgetMusic.tsx`
- Create: `web/src/App.tsx`

- [ ] **Step 1: Create `src/components/WidgetMusic.tsx`**

```typescript
import React from 'react';
import { useWebSocket } from '../context/WebSocketContext';

export const WidgetMusic: React.FC = () => {
  const { media } = useWebSocket();

  return (
    <div className="glass-card" style={{ position: 'absolute', bottom: '18px', right: '18px', left: '18px', padding: '10px 14px', display: 'flex', alignItems: 'center', gap: '12px' }}>
      <div style={{ width: '38px', height: '38px', borderRadius: '8px', background: 'linear-gradient(135deg, #581c87, #c2410c)', border: '1px solid rgba(255, 255, 255, 0.2)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '16px', flexShrink: 0 }}>
        ⚡
      </div>
      <div style={{ flexGrow: 1, minWidth: 0 }}>
        <div style={{ fontFamily: 'var(--font-display)', fontSize: '10px', color: 'var(--text-main)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {media.track}
        </div>
        <div style={{ fontFamily: 'var(--font-geo)', fontSize: '12px', color: 'var(--purple-glow)', marginTop: '2px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {media.artist} • {media.album}
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: '2px', height: '10px', marginTop: '4px' }}>
          <div style={{ flex: 1, background: 'var(--orange-glow)', height: '40%' }} />
          <div style={{ flex: 1, background: 'var(--orange-glow)', height: '80%' }} />
          <div style={{ flex: 1, background: 'var(--orange-glow)', height: '60%' }} />
          <div style={{ flex: 1, background: 'var(--orange-glow)', height: '100%' }} />
          <div style={{ flex: 1, background: 'var(--orange-glow)', height: '50%' }} />
        </div>
      </div>
    </div>
  );
};
```

- [ ] **Step 2: Create `src/App.tsx` Assembling Wall Canvas**

```typescript
import React from 'react';
import { WebSocketProvider } from './context/WebSocketContext';
import { WidgetClock } from './components/WidgetClock';
import { WidgetNodeStatus } from './components/WidgetNodeStatus';
import { WireframeGlobe } from './components/WireframeGlobe';
import { WidgetMusic } from './components/WidgetMusic';
import { SpatialCursor } from './components/SpatialCursor';

export const App: React.FC = () => {
  return (
    <WebSocketProvider>
      <div style={{ width: '100vw', height: '100vh', backgroundColor: '#000000', position: 'relative', overflow: 'hidden' }}>
        <div style={{ position: 'absolute', inset: 0, backgroundImage: 'radial-gradient(rgba(192, 132, 252, 0.08) 1px, transparent 1px)', backgroundSize: '24px 24px', pointerEvents: 'none' }} />
        
        <WidgetClock />
        <WidgetNodeStatus />
        <WireframeGlobe />
        <WidgetMusic />
        <SpatialCursor />
      </div>
    </WebSocketProvider>
  );
};

export default App;
```

- [ ] **Step 3: Commit Task 5**

```bash
git add web/src/components/WidgetMusic.tsx web/src/App.tsx
git commit -m "feat(web): add WidgetMusic and assemble full WallCanvas in App.tsx"
```

---

### Task 6: Build `mock-server.cjs` Vacation Test Harness

**Files:**
- Create: `web/mock-server.cjs`

- [ ] **Step 1: Create `web/mock-server.cjs`**

```javascript
const { WebSocketServer } = require('ws');

const wss = new WebSocketServer({ port: 8080 });
console.log('M.I.R.A. Mock WebSocket Server running on ws://localhost:8080/ws');

wss.on('connection', (ws) => {
  console.log('Client connected');

  let angle = 0;
  const interval = setInterval(() => {
    angle += 0.05;
    const x = 0.5 + 0.25 * Math.sin(angle);
    const y = 0.4 + 0.15 * Math.cos(angle * 0.7);

    ws.send(JSON.stringify({
      type: 'GESTURE_EVENT',
      payload: {
        node_id: 'pi-camera-1',
        timestamp: Date.now(),
        x: parseFloat(x.toFixed(3)),
        y: parseFloat(y.toFixed(3)),
        gesture_type: angle % 3 < 0.2 ? 'PINCH' : 'HOVER'
      }
    }));

    ws.send(JSON.stringify({
      type: 'TELEMETRY_EVENT',
      payload: {
        node_id: 'pi-projector',
        fps: 60,
        status: 'ONLINE'
      }
    }));
  }, 33); // 30 FPS mock update

  ws.on('close', () => clearInterval(interval));
});
```

- [ ] **Step 2: Commit Task 6**

```bash
git add web/mock-server.cjs
git commit -m "feat(web): add mock-server.cjs script for vacation hardware-free testing"
```

---

## Plan Verification

- **Run Dev Server & Mock Server**:
  - Shell 1: `cd web && npm run mock-server`
  - Shell 2: `cd web && npm run dev`
- Open browser at `http://localhost:3000` to verify:
  1. `#000000` true black wall canvas rendering.
  2. `Zen Dots` & `Geo` fonts loaded properly.
  3. 3D Purple Wireframe Globe rotating/floating in center.
  4. Neon Orange reticle moving smoothly along simulated gesture path.
  5. Compact $30'' \times 40''$ wall widget arrangement without voice transcript text popups.
