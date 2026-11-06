// Shared mutable client state (one object, imported by every module). A tab keeps the state only it reads itself.
export const state = {
  activeTab: null,      // the id of the open tab's <section> (tabs.js)
  services: [],         // /api/services (data.js loadServices)
  ratingSources: [],    // /api/rating-sources (data.js loadRatingSources): the library's sort, filter and badges read them
  shows: [],            // the titles the Library shows (its cards read them)
  stats: {},            // /api/stats (data.js loadStats)
  optimizerPlan: null,  // /api/optimizer/plan (the plan's tab fills it; the planner and the header read it)
  drivers: null,        // /api/drivers (drivers.js): the driver of each role and what each offers
  libraryStatus: null,  // /api/drivers/<library driver>/status (library-status.js)
};
