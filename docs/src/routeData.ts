import { defineRouteMiddleware } from '@astrojs/starlight/route-data';
import { PAGES } from './llms';

export const onRequest = defineRouteMiddleware((context) => {
	const route = context.locals.starlightRoute;

	// Pages linked from the header: no left sidebar.
	route.hasSidebar = false;

	// Point agents that fetch the HTML at the page's Markdown copy.
	// Starlight names the home page '' here; its copy is `/index.md`.
	const id = route.entry.id || 'index';
	if (PAGES.includes(id)) {
		route.head.push({
			tag: 'link',
			attrs: { rel: 'alternate', type: 'text/markdown', href: `/${id}.md` },
		});
	}
});
