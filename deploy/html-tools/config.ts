// Our apertium-html-tools config. deploy/deploy.sh copies it over the fork's
// config.ts in a temporary copy of the fork, then builds the frontend.
//
// config.ts is evaluated twice: by Node while building (build.ts fetches the
// pair list, language names etc. from APy and bakes them into the bundle), and
// in the browser. At build time apyURL is a local APy container with the same
// image and pairs archive as the deployed one; in the browser it is /apy on
// whatever domain serves the page, so the build doesn't depend on the domain.
import { Config, Mode } from './src/types';

const building = typeof window === 'undefined';

const buildEnv = (name: string): string => {
  const value = process.env[name];
  if (!value) {
    throw new Error(`config.ts: set ${name} (deploy/deploy.sh does this)`);
  }
  return value;
};

export default {
  defaultLocale: 'eng',
  // Only used at build time, for sitemap.xml
  htmlUrl: building ? buildEnv('HTML_URL') : `${window.location.origin}/`,
  apyURL: building ? buildEnv('APY_BUILD_URL') : `${window.location.origin}/apy`,

  defaultMode: Mode.Translation,
  // The pairs we serve come without separate analyzers/generators
  enabledModes: new Set([Mode.Translation]),
  translationChaining: false,

  // "Post-cleanup with AI": /translateLLM, see deploy/README.md
  llmPostEditing: { model: 'GPT-4.1 mini' },

  stringReplacements: {
    '{{maintainer}}': "<a href='https://giellatekno.uit.no/' target='_blank' rel='noopener'>Giellatekno</a>",
    '{{more_languages}}': "<a href='https://www.apertium.org/' rel='noreferrer' target='_blank'>apertium.org</a>",
  },
  showMoreLanguagesLink: false,
} as Config;
