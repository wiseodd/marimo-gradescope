// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
	site: 'https://mogs.kristia.de',
	integrations: [
		starlight({
			title: 'mogs',
			logo: {
				light: './src/assets/logo-light.svg',
				dark: './src/assets/logo-dark.svg',
				replacesTitle: true,
			},
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/wiseodd/mogs' },
			],
			customCss: ['./src/styles/custom.css'],
			components: {
				Header: './src/components/Header.astro',
				ThemeSelect: './src/components/Empty.astro',
			},
			routeMiddleware: './src/routeData.ts',
			pagefind: false,
			pagination: false,
			tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 2 },
			expressiveCode: {
				themes: ['one-dark-pro', 'one-light'],
				defaultProps: { frame: 'none', wrap: true },
				styleOverrides: { borderRadius: '0.375rem' },
			},
		}),
	],
});
