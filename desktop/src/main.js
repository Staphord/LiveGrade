const path = require('path');
const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  net,
  screen,
  shell,
} = require('electron');
const { autoUpdater } = require('electron-updater');

const BASE_URL = (process.env.LIVEGRADE_DESKTOP_URL || 'https://livegrade.gilgaltech.com').replace(/\/+$/, '');
const DEVPERF_URL = (process.env.LIVEGRADE_DEVPERF_URL || 'https://devperf.gilgaltech.com').replace(/\/+$/, '');
const BASE_ORIGIN = new URL(BASE_URL).origin;
const DEVPERF_ORIGIN = new URL(DEVPERF_URL).origin;

let mainWindow = null;
let projectorWindow = null;
let pendingDeepLink = null;
let offlinePollTimer = null;
let lastOnlineUrl = BASE_URL + '/';

function isProjectorPath(pathname) {
  return /^\/assess\/[^/]+\/screen\/?$/.test(pathname) || /^\/polls\/[^/]+\/qr\/?$/.test(pathname);
}

function isAllowedAppPath(pathname) {
  return (
    pathname === '/' ||
    pathname.startsWith('/assessment/') ||
    pathname.startsWith('/assess/') ||
    pathname.startsWith('/polls/') ||
    pathname.startsWith('/team/') ||
    pathname.startsWith('/showcase/') ||
    pathname.startsWith('/accounts/desktop/exchange/')
  );
}

function createSecureWebPreferences() {
  return {
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    preload: path.join(__dirname, 'preload.js'),
  };
}

function openSystemBrowserLogin(rawUrl) {
  let target = `${BASE_URL}/accounts/desktop/login/`;
  try {
    const parsed = new URL(rawUrl, BASE_URL);
    if (parsed.searchParams.get('signup') === '1') {
      target += '?signup=1';
    }
  } catch (_) {
    // Use default desktop login URL.
  }
  shell.openExternal(target);
}

function shouldInterceptToSystemBrowser(targetUrl) {
  try {
    const parsed = new URL(targetUrl);
    if (parsed.origin === DEVPERF_ORIGIN) return true;
    if (parsed.origin === BASE_ORIGIN) {
      if (parsed.pathname.startsWith('/oidc/authenticate')) return true;
      if (parsed.pathname.startsWith('/accounts/desktop/login')) return true;
    }
  } catch (_) {
    return false;
  }
  return false;
}

function openProjectorWindow(targetUrl) {
  let parsed;
  try {
    parsed = new URL(targetUrl, BASE_URL);
  } catch (_) {
    return;
  }
  if (parsed.origin !== BASE_ORIGIN) return;

  if (projectorWindow && !projectorWindow.isDestroyed()) {
    projectorWindow.loadURL(parsed.toString());
    projectorWindow.focus();
    return;
  }

  const displays = screen.getAllDisplays();
  const primary = screen.getPrimaryDisplay();
  const external = displays.find((d) => d.id !== primary.id);
  const bounds = external ? external.workArea : { width: 1120, height: 760 };

  projectorWindow = new BrowserWindow({
    width: bounds.width,
    height: bounds.height,
    x: external ? bounds.x : undefined,
    y: external ? bounds.y : undefined,
    title: 'LiveGrade Projector',
    autoHideMenuBar: true,
    webPreferences: createSecureWebPreferences(),
  });

  projectorWindow.loadURL(parsed.toString());
  projectorWindow.on('closed', () => {
    projectorWindow = null;
  });
}

function handleDeepLink(rawUrl) {
  if (!rawUrl || !rawUrl.startsWith('livegrade://')) return;
  if (!mainWindow || mainWindow.isDestroyed()) {
    pendingDeepLink = rawUrl;
    return;
  }

  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch (_) {
    return;
  }

  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();

  const routeHost = parsed.hostname;
  if (routeHost === 'auth') {
    const code = parsed.searchParams.get('code') || '';
    if (!code) return;
    const exchangeUrl = `${BASE_URL}/accounts/desktop/exchange/?code=${encodeURIComponent(code)}`;
    mainWindow.loadURL(exchangeUrl);
    return;
  }

  if (routeHost === 'screen') {
    const uuid = parsed.pathname.replace(/^\/+|\/+$/g, '');
    if (uuid) {
      openProjectorWindow(`${BASE_URL}/assess/${encodeURIComponent(uuid)}/screen/`);
    }
    return;
  }

  const candidatePath = `/${routeHost}${parsed.pathname}${parsed.search}`;
  if (isAllowedAppPath(`/${routeHost}${parsed.pathname}`)) {
    mainWindow.loadURL(`${BASE_URL}${candidatePath}`);
  }
}

function startOfflinePolling() {
  if (offlinePollTimer) return;
  offlinePollTimer = setInterval(async () => {
    try {
      const res = await net.fetch(`${BASE_URL}/healthz`, { method: 'GET' });
      if (res.ok && mainWindow && !mainWindow.isDestroyed()) {
        clearInterval(offlinePollTimer);
        offlinePollTimer = null;
        mainWindow.loadURL(lastOnlineUrl || `${BASE_URL}/`);
      }
    } catch (_) {
      // Keep polling until connectivity returns.
    }
  }, 3000);
}

function stopOfflinePolling() {
  if (!offlinePollTimer) return;
  clearInterval(offlinePollTimer);
  offlinePollTimer = null;
}

function buildAppMenu() {
  const template = [
    ...(process.platform === 'darwin'
      ? [{ role: 'appMenu' }]
      : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'Reload Workspace',
          accelerator: 'CmdOrCtrl+R',
          click: () => {
            if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
          },
        },
        {
          label: 'Close Projector Window',
          click: () => {
            if (projectorWindow && !projectorWindow.isDestroyed()) projectorWindow.close();
          },
        },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    title: 'LiveGrade',
    webPreferences: createSecureWebPreferences(),
  });

  mainWindow.webContents.on('will-navigate', (event, targetUrl) => {
    if (shouldInterceptToSystemBrowser(targetUrl)) {
      event.preventDefault();
      openSystemBrowserLogin(targetUrl);
      return;
    }
    try {
      const parsed = new URL(targetUrl);
      if (parsed.origin !== BASE_ORIGIN) {
        event.preventDefault();
        shell.openExternal(targetUrl);
      }
    } catch (_) {
      event.preventDefault();
    }
  });

  mainWindow.webContents.on('will-redirect', (event, targetUrl) => {
    if (shouldInterceptToSystemBrowser(targetUrl)) {
      event.preventDefault();
      openSystemBrowserLogin(targetUrl);
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url: targetUrl }) => {
    try {
      const parsed = new URL(targetUrl);
      if (parsed.origin === BASE_ORIGIN && isProjectorPath(parsed.pathname)) {
        openProjectorWindow(targetUrl);
        return { action: 'deny' };
      }
      if (parsed.origin === BASE_ORIGIN && isAllowedAppPath(parsed.pathname)) {
        return {
          action: 'allow',
          overrideBrowserWindowOptions: {
            width: 1180,
            height: 780,
            webPreferences: createSecureWebPreferences(),
          },
        };
      }
    } catch (_) {
      return { action: 'deny' };
    }
    shell.openExternal(targetUrl);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('did-finish-load', () => {
    const current = mainWindow.webContents.getURL();
    if (current.startsWith(BASE_ORIGIN)) {
      lastOnlineUrl = current;
      stopOfflinePolling();
    }
  });

  mainWindow.webContents.on('did-fail-load', (_event, errorCode, _desc, validatedUrl, isMainFrame) => {
    if (!isMainFrame || errorCode === -3) return;
    if (validatedUrl && validatedUrl.startsWith('livegrade://')) return;
    mainWindow.loadFile(path.join(__dirname, 'offline.html'));
    startOfflinePolling();
  });

  mainWindow.loadURL(`${BASE_URL}/`);

  if (pendingDeepLink) {
    const queued = pendingDeepLink;
    pendingDeepLink = null;
    handleDeepLink(queued);
  }
}

function setupAutoUpdater() {
  if (!app.isPackaged) return;
  autoUpdater.disableWebInstaller = true;
  autoUpdater.on('update-downloaded', async () => {
    const result = await dialog.showMessageBox(mainWindow, {
      type: 'info',
      title: 'Update Ready',
      message: 'A new version of LiveGrade has been downloaded. Restart to apply it now?',
      buttons: ['Restart Now', 'Later'],
      defaultId: 0,
    });
    if (result.response === 0) {
      autoUpdater.quitAndInstall();
    }
  });
  autoUpdater.checkForUpdatesAndNotify().catch(() => {});
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.setAsDefaultProtocolClient('livegrade');

  app.on('second-instance', (_event, commandLine) => {
    const link = commandLine.find((arg) => arg.startsWith('livegrade://'));
    if (link) handleDeepLink(link);
    else if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.on('open-url', (event, url) => {
    event.preventDefault();
    handleDeepLink(url);
  });

  ipcMain.handle('desktop:open-projector', (_event, url) => {
    openProjectorWindow(url);
  });

  ipcMain.handle('desktop:retry-connection', async () => {
    try {
      const res = await net.fetch(`${BASE_URL}/healthz`, { method: 'GET' });
      if (res.ok && mainWindow && !mainWindow.isDestroyed()) {
        stopOfflinePolling();
        mainWindow.loadURL(lastOnlineUrl || `${BASE_URL}/`);
        return true;
      }
    } catch (_) {
      return false;
    }
    return false;
  });

  ipcMain.handle('desktop:get-version', () => app.getVersion());

  app.whenReady().then(() => {
    const initialArg = process.argv.find((arg) => arg.startsWith('livegrade://'));
    if (initialArg) pendingDeepLink = initialArg;
    buildAppMenu();
    createMainWindow();
    setupAutoUpdater();

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    });
  });

  app.on('window-all-closed', () => {
    stopOfflinePolling();
    if (process.platform !== 'darwin') app.quit();
  });
}

