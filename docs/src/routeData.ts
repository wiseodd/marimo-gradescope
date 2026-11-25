import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

// Three pages, linked from the header: no left sidebar.
export const onRequest = defineRouteMiddleware((context) => {
	context.locals.starlightRoute.hasSidebar = false;
});
