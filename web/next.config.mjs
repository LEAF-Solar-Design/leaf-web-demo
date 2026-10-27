import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { engineVersion, findEnginePkgDir } from './vite-plugins/engineVersion.js'
import { elementRefEnabled, environmentDefinitions, sourceIdentity } from './scripts/next-runtime.mjs'

const webRoot = path.dirname(fileURLToPath(import.meta.url))

export default function nextConfig(phase) {
  // Next loads .env* before invoking this configuration function.
  const development = phase === 'phase-development-server'
  return {
    ...(development ? {} : { output: 'export' }),
    reactStrictMode: true,
    images: { unoptimized: true },
    poweredByHeader: false,
    webpack(config, { dev, webpack }) {
      // Vendored contracts and UI sit outside web/. Resolve their dependencies
      // from this app while preserving Next's React aliases and one runtime.
      config.resolve.modules = [path.join(webRoot, 'node_modules'), ...(config.resolve.modules || ['node_modules'])]
      config.plugins.push(new webpack.DefinePlugin({
        ...environmentDefinitions(process.env, dev),
        __BUILD_HASH__: JSON.stringify(sourceIdentity(webRoot)),
        __LEAF_ENGINE_VERSION__: JSON.stringify(engineVersion(findEnginePkgDir(webRoot))),
      }))
      config.module.rules.unshift({
        test: /\.jsx$/, include: path.join(webRoot, 'src'), enforce: 'pre',
        use: [{ loader: path.join(webRoot, 'webpack/elementSourceLoader.cjs'), options: {
          root: webRoot, source: dev, ref: elementRefEnabled(),
        } }],
      }, {
        test: /worker-browser\.mjs$/, include: path.resolve(webRoot, '../vendor'), enforce: 'pre',
        use: [{ loader: path.join(webRoot, 'webpack/elementSourceLoader.cjs'), options: { runtimeImportsOnly: true } }],
      })
      return config
    },
    // Dev deliberately renders the root shell for all extensionless browser
    // routes. The island reads the original window URL; no export params are
    // invented for unknown legacy subpaths. Assets keep normal Next handling.
    ...(development ? { async rewrites() {
      return { beforeFiles: [{
        source: '/:leafPath((?!_next(?:/|$)|engine(?:/|$)|fonts(?:/|$)|.*\\.[^/]+$).+)',
        destination: '/',
      }] }
    } } : {}),
  }
}
