import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import type { BrowserWindow } from 'electron'

import { resolveReadableFileForIpc } from './hardening'

const PREVIEW_WATCH_DEBOUNCE_MS = 120

const previewWatchers = new Map()

type PreviewWatchDeps = {
  getMainWindow: () => BrowserWindow | null
  fileExists: (filePath: string) => boolean
}

let getMainWindow: () => BrowserWindow | null = () => null
let fileExists: (filePath: string) => boolean = () => false

/** One-time DI for main-window send sink + fileExists helper. */
export function initPreviewWatch(deps: PreviewWatchDeps) {
  getMainWindow = deps.getMainWindow
  fileExists = deps.fileExists
}

async function filePathFromPreviewUrl(rawUrl) {
  const { resolvedPath } = await resolveReadableFileForIpc(String(rawUrl || ''), { purpose: 'Preview file' })

  return resolvedPath
}

export function sendPreviewFileChanged(payload) {
  const mainWindow = getMainWindow()

  if (!mainWindow || mainWindow.isDestroyed()) {
    return
  }

  const { webContents } = mainWindow

  if (!webContents || webContents.isDestroyed()) {
    return
  }

  webContents.send('hermes:preview-file-changed', payload)
}

export async function watchPreviewFile(rawUrl) {
  const filePath = await filePathFromPreviewUrl(rawUrl)
  const watchDir = path.dirname(filePath)
  const targetName = path.basename(filePath)
  const id = crypto.randomBytes(12).toString('base64url')
  let timer = null

  const watcher = fs.watch(watchDir, (_eventType, filename) => {
    const changedName = filename ? path.basename(String(filename)) : ''

    if (changedName && changedName !== targetName) {
      return
    }

    if (timer) {
      clearTimeout(timer)
    }

    timer = setTimeout(() => {
      timer = null

      if (!fileExists(filePath)) {
        return
      }

      sendPreviewFileChanged({ id, path: filePath, url: pathToFileURL(filePath).toString() })
    }, PREVIEW_WATCH_DEBOUNCE_MS)
  })

  previewWatchers.set(id, {
    close: () => {
      if (timer) {
        clearTimeout(timer)
      }

      watcher.close()
    }
  })

  return { id, path: filePath }
}

export function stopPreviewFileWatch(id) {
  const watcher = previewWatchers.get(id)

  if (!watcher) {
    return false
  }

  watcher.close()
  previewWatchers.delete(id)

  return true
}

export function closePreviewWatchers() {
  for (const id of previewWatchers.keys()) {
    stopPreviewFileWatch(id)
  }
}
