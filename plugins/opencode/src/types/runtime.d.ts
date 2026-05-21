type Buffer = any
type BufferEncoding = string

declare const Buffer: {
  alloc(size: number): Buffer
  concat(chunks: Buffer[]): Buffer
  from(data: string, encoding?: string): Buffer
}

declare const process: {
  env: Record<string, string | undefined>
  pid: number
  getuid?: () => number
}

declare const require: (name: string) => any
declare function setTimeout(fn: (...args: any[]) => void, ms?: number): any
declare function clearTimeout(timer: any): void

declare module "path" {
  export function join(...parts: string[]): string
  export function basename(path: string): string
  export function dirname(path: string): string
  export function relative(from: string, to: string): string
  export function isAbsolute(path: string): boolean
}

declare module "node:path" {
  export function join(...parts: string[]): string
  export function basename(path: string): string
  export function dirname(path: string): string
  export function relative(from: string, to: string): string
  export function isAbsolute(path: string): boolean
}

declare module "fs" {
  export function existsSync(path: string): boolean
  export function mkdirSync(path: string, options?: { recursive?: boolean; mode?: number }): void
  export function writeFileSync(path: string, data: string, encoding?: string): void
  export function readFileSync(path: string, encoding?: BufferEncoding): string
  export function readdirSync(path: string): string[]
  export function statSync(path: string): { mtimeMs: number; mode: number; uid: number; isSocket?: () => boolean }
  export function chmodSync(path: string, mode: number): void
  export function unlinkSync(path: string): void
  export function renameSync(oldPath: string, newPath: string): void
  export function appendFileSync(path: string, data: string): void
}

declare module "node:fs" {
  export function existsSync(path: string): boolean
  export function mkdirSync(path: string, options?: { recursive?: boolean; mode?: number }): void
  export function writeFileSync(path: string, data: string, encoding?: string): void
  export function readFileSync(path: string, encoding?: BufferEncoding): string
  export function readdirSync(path: string): string[]
  export function statSync(path: string): { mtimeMs: number; mode: number; uid: number; isSocket?: () => boolean }
  export function chmodSync(path: string, mode: number): void
  export function unlinkSync(path: string): void
  export function renameSync(oldPath: string, newPath: string): void
  export function appendFileSync(path: string, data: string): void
}

declare module "os" {
  export function homedir(): string
}

declare module "process" {
  export const platform: string
}

declare module "crypto" {
  export function createHash(algorithm: string): {
    update(data: string): { digest(encoding: string): string }
    digest(encoding: string): string
  }
}

declare module "node:crypto" {
  export function createHash(algorithm: string): {
    update(data: string): { digest(encoding: string): string }
    digest(encoding: string): string
  }
  export function randomUUID(): string
}

declare module "net" {
  interface Socket {
    on(event: string, callback: (...args: any[]) => void): Socket
    write(data: string): void
    destroy(): void
  }
  export function connect(path: string): Socket
}

declare module "bun" {
  export interface Subprocess {
    pid: number
    exited: Promise<number>
    kill(signal?: string): void
  }
  export function spawn(options: {
    cmd: string[]
    stdout?: string
    stderr?: string
    detached?: boolean
    env?: Record<string, string | undefined>
  }): Subprocess
  export function spawn(command: string[], options?: Record<string, unknown>): Subprocess
}

declare const Bun: {
  write(path: string, data: string | Uint8Array, options?: { create?: boolean; append?: boolean }): Promise<unknown>
  file(path: string): {
    exists(): Promise<boolean>
    text(): Promise<string>
  }
}

declare module "better-sqlite3" {
  namespace BetterSqlite3 {
    interface RunResult {
      changes: number
      lastInsertRowid: number | bigint
    }
    interface Statement {
      run(...params: any[]): RunResult
      get(...params: any[]): any
      all(...params: any[]): any[]
    }
    interface Database {
      pragma(sql: string): unknown
      close(): void
      prepare(sql: string): Statement
      exec(sql: string): unknown
      transaction<T extends (...args: any[]) => any>(fn: T): T
    }
  }
  class BetterSqlite3 implements BetterSqlite3.Database {
    constructor(path: string, options?: Record<string, unknown>)
    pragma(sql: string): unknown
    close(): void
    prepare(sql: string): BetterSqlite3.Statement
    exec(sql: string): unknown
    transaction<T extends (...args: any[]) => any>(fn: T): T
  }
  export = BetterSqlite3
}
