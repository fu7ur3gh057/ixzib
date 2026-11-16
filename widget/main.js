const { app, BrowserWindow, globalShortcut, Tray, nativeImage, screen, ipcMain, Menu } = require('electron');
const path = require('path');
const fs = require('fs');
const os = require('os');
const { execFile } = require('child_process');

let win, tray;

function createWindow() {
  const { width } = screen.getPrimaryDisplay().workAreaSize;
  win = new BrowserWindow({
    width: 300, height: 360,
    x: width - 340, y: 50,
    frame: false, transparent: true, hasShadow: false, backgroundColor: '#00000000',
    type: 'panel',               // NSPanel: floats, never steals focus, survives app switching
    alwaysOnTop: true, resizable: false, skipTaskbar: true,
    hiddenInMissionControl: true,
    acceptFirstMouse: true,
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js'), backgroundThrottling: false },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setHiddenInMissionControl?.(true);
  win.webContents.on('console-message', (_e, ...a) => console.log('[renderer]', a[0]?.message ?? a[1]));
  // hover detection by cursor position (drag regions don't deliver mouse events)
  let hov = false;
  setInterval(() => {
    if (!win || win.isDestroyed() || !win.isVisible()) return;
    const p = screen.getCursorScreenPoint(), b = win.getBounds();
    const inside = p.x >= b.x + 60 && p.x <= b.x + 240 && p.y >= b.y && p.y <= b.y + 185;
    if (inside !== hov) { hov = inside; win.webContents.send('hover', hov); }
  }, 60);
  win.loadFile(process.env.IXZIB_SKIN || 'index.html');
  if (process.env.IXZIB_OPEN_CHAT === '1') win.webContents.once('did-finish-load', () => setTimeout(openChat, 400));
}

ipcMain.on('quit', () => app.quit());

// ---------- open the Ixzib chat (history + keyboard input) in a terminal ----------
const CORE_DIR = path.resolve(__dirname, '..', 'core');
const CHAT_CMD = `${path.join(CORE_DIR, '.venv', 'bin', 'python')} ${path.join(CORE_DIR, 'ixzib_chat.py')}`;
const asStr = (x) => '"' + String(x).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
const osa = (script) => execFile('osascript', ['-e', script], (err) => err && console.log('[osascript]', err.message));

function openInTerminal() {
  osa(`tell application "Terminal"\nactivate\ndo script ${asStr(CHAT_CMD)}\nend tell`);
}
function openInGhostty() {
  // wrap in sh -c: Ghostty runs commands as a login shell (argv[0] gets a leading '-'), which breaks venv detection
  const cfg = `{command:${asStr(`/bin/sh -c '${CHAT_CMD}'`)}, initial working directory:${asStr(CORE_DIR)}}`;
  execFile('pgrep', ['-x', 'ghostty'], (notRunning) => {
    // already open -> new tab in the existing window; otherwise start it
    osa(`tell application "Ghostty"\nactivate\n${notRunning ? 'delay 1\n' : ''}if (count of windows) > 0 then\nnew tab in front window with configuration ${cfg}\nelse\nnew window with configuration ${cfg}\nend if\nend tell`);
  });
}
// ---------- native glass chat window ----------
let chatWin = null;
function openChat() {
  if (chatWin && !chatWin.isDestroyed()) {
    chatWin.show(); app.focus({ steal: true }); chatWin.focus();
    return;
  }
  const b = win.getBounds();
  const d = screen.getDisplayMatching(b).workArea;
  const W = 420, H = 640;
  let x = b.x - W - 12;                                         // prefer the left of the orb
  if (x < d.x + 8) x = Math.min(b.x + b.width + 12, d.x + d.width - W - 8);
  const y = Math.min(Math.max(b.y, d.y + 8), d.y + d.height - H - 8);
  chatWin = new BrowserWindow({
    width: W, height: H, x, y, minWidth: 340, minHeight: 420,
    frame: false, transparent: true, backgroundColor: '#00000000', hasShadow: true,
    vibrancy: 'hud', visualEffectState: 'active', roundedCorners: true,   // real macOS glass blur behind the window
    show: false, resizable: true, skipTaskbar: true, alwaysOnTop: true,
    webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js'), backgroundThrottling: false },
  });
  chatWin.setAlwaysOnTop(true, 'floating');
  chatWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  chatWin.webContents.on('console-message', (_e, ...a) => console.log('[chat]', a[0]?.message ?? a[1]));
  chatWin.once('ready-to-show', () => { console.log('[chat] window shown'); chatWin.show(); app.focus({ steal: true }); chatWin.focus(); });
  chatWin.loadFile('chat.html');
}
ipcMain.on('chat-close', () => { if (chatWin && !chatWin.isDestroyed()) chatWin.hide(); });

const HAS_GHOSTTY = ['/Applications/Ghostty.app', path.join(os.homedir(), 'Applications', 'Ghostty.app')].some((p) => fs.existsSync(p));

function showMenu() {
  Menu.buildFromTemplate([
    { label: 'Открыть', click: openChat },
    { label: 'Открыть с помощью', submenu: [
      ...(HAS_GHOSTTY ? [{ label: 'Ghostty', click: openInGhostty }] : []),
      { label: 'Terminal', click: openInTerminal },
    ] },
    { label: 'Вид', submenu: [{ label: 'Скоро…', enabled: false }] },
    { type: 'separator' },
    { label: 'Закрыть', click: () => app.quit() },
  ]).popup({ window: win });
}
ipcMain.on('menu', showMenu);

// manual window drag (the CSS drag region would swallow clicks)
let dragOrigin = null;
ipcMain.on('drag-start', () => { dragOrigin = win.getPosition(); });
ipcMain.on('drag-move', (_e, dx, dy) => { if (dragOrigin) win.setPosition(Math.round(dragOrigin[0] + dx), Math.round(dragOrigin[1] + dy)); });

app.whenReady().then(() => {
  if (process.platform === 'darwin') app.setActivationPolicy('accessory'); // no Dock icon, not in Cmd+Tab
  createWindow();
  tray = new Tray(nativeImage.createEmpty());
  tray.setTitle('◉');
  tray.on('click', () => (win.isVisible() ? win.hide() : win.showInactive()));
  globalShortcut.register('Alt+Space', () => (win.isVisible() ? win.hide() : win.showInactive()));
  globalShortcut.register('Alt+Shift+Space', () => win.webContents.send('toggle'));
});

app.on('window-all-closed', () => app.quit());
