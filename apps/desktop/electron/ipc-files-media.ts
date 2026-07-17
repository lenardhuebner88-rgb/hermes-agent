import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import https from 'node:https'
import path from 'node:path'

import type { BrowserWindow } from 'electron'
import { app, clipboard, dialog, nativeImage } from 'electron'

import { DATA_URL_READ_MAX_BYTES, resolveReadableFileForIpc, TEXT_PREVIEW_SOURCE_MAX_BYTES } from './hardening'
import { readWslWindowsClipboardImage } from './wsl-clipboard-image'
import { resolvePickerDefaultPath } from './wsl-path-bridge'

const MEDIA_MIME_TYPES = {
  '.avi': 'video/x-msvideo',
  '.bmp': 'image/bmp',
  '.flac': 'audio/flac',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.m4a': 'audio/mp4',
  '.mkv': 'video/x-matroska',
  '.mov': 'video/quicktime',
  '.mp3': 'audio/mpeg',
  '.mp4': 'video/mp4',
  '.ogg': 'audio/ogg',
  '.opus': 'audio/ogg; codecs=opus',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.wav': 'audio/wav',
  '.webm': 'video/webm',
  '.webp': 'image/webp'
}

type IpcFilesMediaDeps = {
  getMainWindow: () => BrowserWindow | null
  isWindows: boolean
  isWsl: boolean
  previewLanguageByExt: Record<string, string>
  textPreviewMaxBytes: number
}

let getMainWindow: () => BrowserWindow | null = () => null
let isWindows = false
let isWsl = false
let previewLanguageByExt: Record<string, string> = {}
let textPreviewMaxBytes = 512 * 1024

/** One-time DI for main-window dialogs + platform/preview maps. */
export function initIpcFilesMedia(deps: IpcFilesMediaDeps) {
  getMainWindow = deps.getMainWindow
  isWindows = deps.isWindows
  isWsl = deps.isWsl
  previewLanguageByExt = deps.previewLanguageByExt
  textPreviewMaxBytes = deps.textPreviewMaxBytes
}

export function looksBinary(buffer) {
  if (!buffer.length) {
    return false
  }

  let suspicious = 0

  for (const byte of buffer) {
    if (byte === 0) {
      return true
    }

    // Allow common whitespace controls: tab, LF, CR.
    if (byte < 32 && byte !== 9 && byte !== 10 && byte !== 13) {
      suspicious += 1
    }
  }

  return suspicious / buffer.length > 0.12
}

export function mimeTypeForPath(filePath) {
  const ext = path.extname(filePath || '').toLowerCase()

  return MEDIA_MIME_TYPES[ext] || 'application/octet-stream'
}

function extensionForMimeType(mimeType) {
  const type = String(mimeType || '')
    .split(';')[0]
    .trim()
    .toLowerCase()

  if (type === 'image/png') {
    return '.png'
  }

  if (type === 'image/jpeg') {
    return '.jpg'
  }

  if (type === 'image/gif') {
    return '.gif'
  }

  if (type === 'image/webp') {
    return '.webp'
  }

  if (type === 'image/bmp') {
    return '.bmp'
  }

  if (type === 'image/svg+xml') {
    return '.svg'
  }

  return ''
}

function filenameFromUrl(rawUrl, fallback = 'image') {
  try {
    const parsed = new URL(rawUrl)
    const base = path.basename(decodeURIComponent(parsed.pathname || ''))

    return base && base.includes('.') ? base : fallback
  } catch {
    return fallback
  }
}

async function resourceBufferFromUrl(rawUrl) {
  if (!rawUrl) {
    throw new Error('Missing URL')
  }

  if (rawUrl.startsWith('data:')) {
    const match = rawUrl.match(/^data:([^;,]+)?(;base64)?,(.*)$/s)

    if (!match) {
      throw new Error('Invalid data URL')
    }

    const mimeType = match[1] || 'application/octet-stream'
    const encoded = match[3] || ''
    const buffer = match[2] ? Buffer.from(encoded, 'base64') : Buffer.from(decodeURIComponent(encoded), 'utf8')

    return { buffer, mimeType }
  }

  if (/^file:/i.test(rawUrl)) {
    const { resolvedPath } = await resolveReadableFileForIpc(rawUrl, { purpose: 'Image file' })
    const buffer = await fs.promises.readFile(resolvedPath)

    return { buffer, mimeType: mimeTypeForPath(resolvedPath) }
  }

  const parsed = new URL(rawUrl)
  const client = parsed.protocol === 'https:' ? https : http

  return new Promise((resolve, reject) => {
    const req = client.get(parsed, res => {
      if ((res.statusCode || 500) >= 400) {
        reject(new Error(`Failed to fetch ${rawUrl}: ${res.statusCode}`))
        res.resume()

        return
      }

      const chunks = []
      res.on('error', reject)
      res.on('data', chunk => chunks.push(chunk))
      res.on('end', () => {
        resolve({
          buffer: Buffer.concat(chunks),
          mimeType: res.headers['content-type'] || 'application/octet-stream'
        })
      })
    })

    req.on('error', reject)
  })
}

export async function copyImageFromUrl(rawUrl) {
  const { buffer } = (await resourceBufferFromUrl(rawUrl)) as any
  const image = nativeImage.createFromBuffer(buffer)

  if (image.isEmpty()) {
    throw new Error('Could not read image')
  }

  clipboard.writeImage(image)
}

export async function saveImageFromUrl(rawUrl) {
  const { buffer, mimeType } = (await resourceBufferFromUrl(rawUrl)) as any
  const fallbackName = filenameFromUrl(rawUrl, `image${extensionForMimeType(mimeType) || '.png'}`)

  const result = await dialog.showSaveDialog(getMainWindow(), {
    title: 'Save Image',
    defaultPath: fallbackName
  })

  if (result.canceled || !result.filePath) {
    return false
  }

  await fs.promises.writeFile(result.filePath, buffer)

  return true
}

export async function writeComposerImage(buffer, ext = '.png') {
  const rawExt = String(ext || '.png')
    .trim()
    .toLowerCase()

  const normalizedExt = rawExt.startsWith('.') ? rawExt : `.${rawExt}`
  const safeExt = /^\.[a-z0-9]{1,5}$/.test(normalizedExt) ? normalizedExt : '.png'
  const dir = path.join(app.getPath('userData'), 'composer-images')
  await fs.promises.mkdir(dir, { recursive: true })
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').replace('Z', '')
  const random = crypto.randomBytes(3).toString('hex')
  const filePath = path.join(dir, `composer_${stamp}_${random}${safeExt}`)
  await fs.promises.writeFile(filePath, buffer)

  return filePath
}

export async function readFileDataUrl(filePath) {
  const { resolvedPath } = await resolveReadableFileForIpc(filePath, {
    maxBytes: DATA_URL_READ_MAX_BYTES,
    purpose: 'File preview'
  })

  const data = await fs.promises.readFile(resolvedPath)

  return `data:${mimeTypeForPath(resolvedPath)};base64,${data.toString('base64')}`
}

export async function readFileText(filePath) {
  const { resolvedPath, stat } = await resolveReadableFileForIpc(filePath, {
    maxBytes: TEXT_PREVIEW_SOURCE_MAX_BYTES,
    purpose: 'Text preview'
  })

  const ext = path.extname(resolvedPath).toLowerCase()
  const handle = await fs.promises.open(resolvedPath, 'r')
  const bytesToRead = Math.min(stat.size, textPreviewMaxBytes)

  try {
    const buffer = Buffer.alloc(bytesToRead)
    const { bytesRead } = await handle.read(buffer, 0, bytesToRead, 0)

    return {
      binary: looksBinary(buffer.subarray(0, Math.min(bytesRead, 4096))),
      byteSize: stat.size,
      language: previewLanguageByExt[ext] || 'text',
      mimeType: mimeTypeForPath(resolvedPath),
      path: resolvedPath,
      text: buffer.subarray(0, bytesRead).toString('utf8'),
      truncated: stat.size > textPreviewMaxBytes
    }
  } finally {
    await handle.close()
  }
}

export async function selectPaths(options: any = {}) {
  const properties = options?.directories ? ['openDirectory'] : ['openFile']

  if (options?.multiple !== false) {
    properties.push('multiSelections')
  }

  let resolvedDefaultPath

  if (options?.defaultPath) {
    try {
      // On a Windows host with a WSL backend the cwd may be a POSIX/WSL path;
      // bridge it to a UNC/drive form the native dialog can actually open.
      const bridged = isWindows ? resolvePickerDefaultPath(String(options.defaultPath)) : String(options.defaultPath)
      resolvedDefaultPath = bridged ? path.resolve(bridged) : undefined
    } catch {
      resolvedDefaultPath = undefined
    }
  }

  const result = await dialog.showOpenDialog(getMainWindow(), {
    title: options?.title || 'Add context',
    defaultPath: resolvedDefaultPath,
    properties: properties as any,
    filters: Array.isArray(options?.filters) ? options.filters : undefined
  })

  if (result.canceled) {
    return []
  }

  return result.filePaths
}

export function writeClipboard(text) {
  clipboard.writeText(String(text || ''))

  return true
}

export async function saveImageBuffer(payload) {
  const data = payload?.data

  if (!data) {
    throw new Error('saveImageBuffer: missing data')
  }

  const buffer = Buffer.isBuffer(data) ? data : Buffer.from(data)

  return writeComposerImage(buffer, payload?.ext || '.png')
}

export async function saveClipboardImage() {
  const image = clipboard.readImage()

  if (image && !image.isEmpty()) {
    return writeComposerImage(image.toPNG(), '.png')
  }

  // WSL2/WSLg doesn't bridge clipboard *images* from the Windows host to the
  // Linux clipboard Electron reads, so a host screenshot looks empty above.
  // Pull it straight off the Windows clipboard via PowerShell as a fallback.
  if (isWsl) {
    const png = readWslWindowsClipboardImage()

    if (png) {
      return writeComposerImage(png, '.png')
    }
  }

  return ''
}
