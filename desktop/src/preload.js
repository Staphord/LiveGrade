const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('livegradeDesktop', {
  openProjector: (url) => ipcRenderer.invoke('desktop:open-projector', url),
  retryConnection: () => ipcRenderer.invoke('desktop:retry-connection'),
  getAppVersion: () => ipcRenderer.invoke('desktop:get-version'),
});

