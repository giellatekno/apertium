// Serves the frontend and runs APy in a container. See deploy/README.md.
//
//   /apy/*           → the APy container (prefix stripped)
//   POST /admin/restart  (Authorization: Bearer $ADMIN_TOKEN)
//                    → restart APy, e.g. after uploading new language pairs
//   everything else  → static assets (the frontend)
//
// The container has no internet access. Its only way out is two made-up
// hostnames, which the Durable Object intercepts and handles here:
//   pairs.internal   → PairsHandler: the current version of each language pair, from R2
//   llm.internal     → LlmProxy: the UiT LLM gateway, with the API key added
//                      here, so the container never has the key

import { DurableObject, WorkerEntrypoint } from 'cloudflare:workers';

const APY_PORT = 2737;
// One instance. Name it differently to start a fresh one (or several).
const APY_INSTANCE = 'apy-0';
// CPU is billed by actual use, memory and disk by what's provisioned.
// APy with sme-nob uses ~0.5 GB; this leaves room for more pairs.
const APY_RESOURCES = { vcpu: 1, memoryMib: 2048, diskMb: 4000 };
// Stop the container after this long without requests (max 6 h).
const IDLE_TIMEOUT_MS = 6 * 60 * 60 * 1000;
// How long to wait for APy to answer after starting the container.
const STARTUP_TIMEOUT_MS = 60_000;

export class Apy extends DurableObject<Env> {
  // Shared by concurrent requests while the container starts
  private ready?: Promise<void>;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const container = ctx.container;
    if (container?.running) {
      // The Durable Object restarted while the container kept running
      void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(IDLE_TIMEOUT_MS));
      this.watchExit(container);
    }
  }

  private get container(): Container {
    if (!this.ctx.container) throw new Error('No container configured for this Durable Object');
    return this.ctx.container;
  }

  async fetch(request: Request): Promise<Response> {
    try {
      await (this.ready ??= this.startAndWait());
    } catch (error) {
      this.ready = undefined;
      console.error('APy failed to start:', error);
      return new Response('Translation service is starting or unavailable, try again shortly.', { status: 503 });
    }
    const url = new URL(request.url);
    return this.container.getTcpPort(APY_PORT).fetch(new Request(`http://container${url.pathname}${url.search}`, request));
  }

  // Stops the container; the next request starts a new one, which fetches the
  // current language pairs.
  async restart(): Promise<void> {
    this.ready = undefined;
    if (this.container.running) await this.container.destroy();
  }

  private async startAndWait(): Promise<void> {
    const container = this.container;
    if (!container.running) {
      // Intercepts must be registered before start(), and again for each new container
      await container.interceptOutboundHttp('pairs.internal', this.ctx.exports.PairsHandler);
      await container.interceptOutboundHttp('llm.internal', this.ctx.exports.LlmProxy);

      const llm = new URL(this.env.LLM_ENDPOINT);
      container.start({
        image: container.images.apy,
        instance: APY_RESOURCES,
        enableInternet: false,
        env: {
          PAIRS: this.env.PAIRS,
          PAIRS_URL: 'http://pairs.internal',
          APY_LLM_ENDPOINT: `http://llm.internal${llm.pathname}${llm.search}`,
          // APy needs some key to enable /translateLLM; LlmProxy replaces it
          APY_LLM_API_KEY: 'added-by-worker',
        },
      });
      this.watchExit(container);
    }
    await container.setInactivityTimeout(IDLE_TIMEOUT_MS);

    // `running` only means startup was requested; wait until APy answers
    const deadline = Date.now() + STARTUP_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (!container.running) throw new Error('APy container stopped during startup');
      try {
        const response = await container
          .getTcpPort(APY_PORT)
          .fetch('http://container/listPairs', { signal: AbortSignal.timeout(2000) });
        if (response.ok) return;
      } catch {
        // Not listening yet
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error(`APy did not answer on port ${APY_PORT} within ${STARTUP_TIMEOUT_MS / 1000} s`);
  }

  private watchExit(container: Container): void {
    this.ctx.waitUntil(
      container
        .monitor()
        .then(() => console.log('APy container stopped'))
        .catch((error) => console.error('APy container exited:', error))
        .finally(() => (this.ready = undefined)),
    );
  }
}

// http://pairs.internal/<pair>.tar.zst → R2 pairs/<pair>/<version>.tar.zst,
// where pairs/<pair>/current names the version. Only this instance's pairs.
export class PairsHandler extends WorkerEntrypoint<Env> {
  async fetch(request: Request): Promise<Response> {
    const pair = new URL(request.url).pathname.match(/^\/([a-z]{3}-[a-z]{3})\.tar\.zst$/)?.[1];
    if (request.method !== 'GET' || !pair || !this.env.PAIRS.split(/\s+/).includes(pair)) {
      return new Response('Not found', { status: 404 });
    }
    const version = (await (await this.env.PAIRS_BUCKET.get(`pairs/${pair}/current`))?.text())?.trim();
    const archive = version ? await this.env.PAIRS_BUCKET.get(`pairs/${pair}/${version}.tar.zst`) : null;
    if (!archive) {
      console.error(`Language pair ${pair} not found in R2 (current: ${version ?? 'missing'})`);
      return new Response('Language pair not found', { status: 404 });
    }
    console.log(`Serving language pair ${pair} ${version}`);
    return new Response(archive.body, { headers: { 'content-length': String(archive.size) } });
  }
}

// http://llm.internal/<path of LLM_ENDPOINT> → LLM_ENDPOINT, with the API key.
// Allows only that one request type, so a compromised container can't use the
// key for anything else, and can't see it.
export class LlmProxy extends WorkerEntrypoint<Env> {
  async fetch(request: Request): Promise<Response> {
    const upstream = new URL(this.env.LLM_ENDPOINT);
    const url = new URL(request.url);
    if (request.method !== 'POST' || url.hostname !== 'llm.internal' || url.pathname !== upstream.pathname) {
      return new Response('Forbidden', { status: 403 });
    }
    return fetch(upstream, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'api-key': this.env.APY_LLM_API_KEY },
      body: request.body,
      redirect: 'manual',
    });
  }
}

const timingSafeEqual = (a: string, b: string): boolean => {
  const encoder = new TextEncoder();
  const [x, y] = [encoder.encode(a), encoder.encode(b)];
  return x.byteLength === y.byteLength && crypto.subtle.timingSafeEqual(x, y);
};

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === '/apy' || url.pathname.startsWith('/apy/')) {
      url.pathname = url.pathname.slice('/apy'.length) || '/';
      return env.APY.getByName(APY_INSTANCE).fetch(new Request(url, request));
    }

    if (url.pathname === '/admin/restart') {
      const token = request.headers.get('authorization')?.replace(/^Bearer /, '') ?? '';
      if (request.method !== 'POST' || !env.ADMIN_TOKEN || !timingSafeEqual(token, env.ADMIN_TOKEN)) {
        return new Response('Forbidden', { status: 403 });
      }
      await env.APY.getByName(APY_INSTANCE).restart();
      return new Response('APy restarted; the next request starts it with the current language pairs.\n');
    }

    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Env>;
