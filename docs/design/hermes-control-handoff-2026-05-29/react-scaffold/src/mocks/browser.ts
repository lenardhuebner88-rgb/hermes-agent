/**
 * MSW-Browser-Worker (Dev-Only). In der App früh starten:
 *
 *   if (process.env.NODE_ENV === 'development' && useMocks) {
 *     const { startMocks } = await import('@/mocks/browser');
 *     await startMocks();
 *   }
 *
 * Benötigt einmalig:  npx msw init public/ --save
 */
import { setupWorker } from 'msw/browser';
import { handlers } from './handlers';

export const worker = setupWorker(...handlers);

export const startMocks = () =>
  worker.start({ onUnhandledRequest: 'bypass', quiet: false });
