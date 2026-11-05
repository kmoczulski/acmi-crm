// Shared map layers for land search and the site page's mini map: base maps, live overlays (power, water,
// flood, zoning...) and incentive-program layers. Needs Leaflet and esri-leaflet loaded first.
(function () {
  const PUCT = 'https://services6.arcgis.com/N6Lzvtb46cpxThhu/arcgis/rest/services/';
  const CHARLES = 'https://services7.arcgis.com/3BMWkdyrt45RNCrq/arcgis/rest/services/';
  const NTAD = 'https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/';
  const TX_PARCELS = 'https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer';
  const HSV = 'https://maps.huntsvilleal.gov/server/rest/services/';
  const TIGER_PLACES = 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4';
  // City of Huntsville zone codes -> map colour (industrial purple, research park blue, commercial red, homes yellow).
  const hsvZoneColor = z => /^(PI|CIP|AIP|IP|LI|HI)$/.test(z) ? '#8e44ad' : /^RP/.test(z) ? '#2e86de' : /^(C\d|O)$/.test(z) ? '#e74c3c'
    : /^(R\d|RA)/.test(z) ? '#f1c40f' : '#27ae60';

  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])); }
  const popup = fields => layer => {
    const p = layer.feature.properties;
    return fields.map(([k, label]) => p[k] != null && String(p[k]).trim() ? `<b>${label}</b> ${escapeHtml(p[k])}` : '').filter(Boolean).join('<br>') || 'No details';
  };
  const voltColor = v => /500|735|345/.test(v || '') ? '#d7191c' : /220|230|287/.test(v || '') ? '#fdae61' : '#ffd92f';

  // A regional layer (one state's parcels, one county's zoning) joins the map only while the view touches its area:
  // a Texas export over Maryland has nothing to draw and would still cost a request on every move.
  const TX = [[25.8, -106.7], [36.6, -93.5]], CHARLES_CO = [[38.1, -77.4], [38.8, -76.5]];
  const HSV_AREA = [[34.1, -87.4], [35.1, -86.1]], ALABAMA = [[30.1, -88.6], [35.1, -84.8]];
  function regional(layer, corners) {
    const area = L.latLngBounds(corners);
    const group = L.layerGroup();
    const sync = () => {
      const on = !!group._map && group._map.getBounds().intersects(area);
      if (on && !group.hasLayer(layer)) group.addLayer(layer);
      else if (!on && group.hasLayer(layer)) group.removeLayer(layer);
    };
    group.on('add', () => { group._map.on('moveend', sync); sync(); });
    group.on('remove', () => { if (group._map) group._map.off('moveend', sync); });
    return group;
  }

  function bases() {
    return {
      satellite: L.layerGroup([
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {maxZoom: 19, attribution: 'Imagery © Esri, Maxar, Earthstar'}),
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {maxZoom: 19}),
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}', {maxZoom: 19, opacity: .7}),
      ]),
      streets: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 19, attribution: '© OpenStreetMap contributors'}),
      topo: L.tileLayer('https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}', {maxZoom: 16, attribution: 'USGS'}),
    };
  }

  // Overlay factories: key -> () => layer. Layers are built on first use.
  function overlays({femaLayer = 35, programs = []} = {}) {
    const o = {
      lines: () => L.layerGroup([
        regional(L.esri.dynamicMapLayer({url: TX_PARCELS, minZoom: 14, opacity: .8}), TX),
        regional(L.esri.featureLayer({url: CHARLES + 'Tax_Parcels/FeatureServer/0', minZoom: 15, fields: ['FID'], interactive: false,
          style: () => ({color: '#ffffff', weight: 1, fill: false, opacity: .8})}), CHARLES_CO),
        // The city's server sends no CORS headers: ask for the picture itself (f: image) instead of a JSON reply.
        regional(L.esri.dynamicMapLayer({url: HSV + 'Boundaries/CombinedParcels/MapServer', layers: [7, 8, 10], minZoom: 14, opacity: .8, f: 'image'}), HSV_AREA),
      ]),
      transmission: () => L.esri.featureLayer({url: 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        minZoom: 8, simplifyFactor: .5, precision: 5, style: f => ({color: voltColor(f.properties.VOLT_CLASS), weight: 3})})
        .bindPopup(popup([['VOLT_CLASS', 'kV'], ['OWNER', 'Owner'], ['SUB_1', 'From'], ['SUB_2', 'To']])),
      substations: () => L.esri.featureLayer({url: 'https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/Electric_Substations/FeatureServer/0',
        minZoom: 9, pointToLayer: (f, ll) => L.circleMarker(ll, {radius: 6, color: '#111', weight: 1.5, fillColor: '#ffd92f', fillOpacity: 1})})
        .bindPopup(popup([['NAME', 'Substation'], ['MAX_VOLTAG', 'Max kV'], ['MIN_VOLTAG', 'Min kV'], ['LINES', 'Lines']])),
      electric: () => L.layerGroup([
        ...[['IOU/FeatureServer/300', '#e7298a'], ['COOP_DIST/FeatureServer/310', '#66a61e'], ['MUNI/FeatureServer/320', '#7570b3']].map(([path, color]) =>
          L.esri.featureLayer({url: PUCT + path, simplifyFactor: .8, precision: 4, style: () => ({color, weight: 1.5, fillOpacity: .08})})
            .bindPopup(popup([['COMPANY_NAME', 'Electric'], ['COMPANY_TYPE', 'Type'], ['MAIN_PHONE', 'Phone'], ['COMPANY_WEBSITE', 'Web']]))),
        L.esri.featureLayer({url: 'https://services6.arcgis.com/BAJNi3EgCdtQ1BCG/arcgis/rest/services/Electric_Retail_Service_Territories/FeatureServer/0',
          where: "STATE IN ('MD','VA','DC','AL','TN','NC','AZ','IN','FL','UT','HI')", simplifyFactor: .8, precision: 4, style: () => ({color: '#66a61e', weight: 1.5, fillOpacity: .06})})
          .bindPopup(popup([['NAME', 'Electric'], ['TYPE', 'Type'], ['CNTRL_AREA', 'Grid']])),
      ]),
      gas: () => L.esri.featureLayer({url: 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0',
        minZoom: 8, style: () => ({color: '#a6761d', weight: 2.5, dashArray: '6 4'})}).bindPopup(popup([['Operator', 'Operator'], ['TYPEPIPE', 'Type']])),
      water: () => L.layerGroup([
        L.esri.featureLayer({url: PUCT + 'Water_CCN_Service_Areas/FeatureServer/210', minZoom: 9, simplifyFactor: .5, precision: 5,
          style: () => ({color: '#1f78b4', weight: 1, fillOpacity: .15})}).bindPopup(popup([['UTILITY', 'Water'], ['DBA_NAME', 'DBA'], ['CCN_NO', 'CCN']])),
        L.esri.featureLayer({url: CHARLES + 'Water_and_Sewer_Category_Designations/FeatureServer/1', minZoom: 9,
          style: f => ({color: '#1f78b4', weight: 1, fillOpacity: /^W[123]/.test(f.properties.Category || '') ? .25 : .05})}).bindPopup(popup([['Category', 'Water category']])),
      ]),
      sewer: () => L.layerGroup([
        L.esri.featureLayer({url: PUCT + 'Sewer_CCN_Service_Areas/FeatureServer/230', minZoom: 9, simplifyFactor: .5, precision: 5,
          style: () => ({color: '#6a3d9a', weight: 1, fillOpacity: .15})}).bindPopup(popup([['UTILITY', 'Sewer'], ['DBA_NAME', 'DBA'], ['CCN_NO', 'CCN']])),
        L.esri.featureLayer({url: CHARLES + 'Water_and_Sewer_Category_Designations/FeatureServer/0', minZoom: 9,
          style: f => ({color: '#6a3d9a', weight: 1, fillOpacity: /^S[123]/.test(f.properties.Category || '') ? .25 : .05})}).bindPopup(popup([['Category', 'Sewer category']])),
      ]),
      waterlines: () => L.layerGroup([
        L.esri.featureLayer({url: PUCT + 'Water_CCN_Facility_Lines/FeatureServer/200', minZoom: 12, style: () => ({color: '#33a0ff', weight: 2})}).bindPopup(popup([['UTILITY', 'Water line'], ['FACILITY', 'Facility']])),
        L.esri.featureLayer({url: PUCT + 'Sewer_CCN_Facility_Lines/FeatureServer/220', minZoom: 12, style: () => ({color: '#b15928', weight: 2})}).bindPopup(popup([['UTILITY', 'Sewer line'], ['FACILITY', 'Facility']])),
      ]),
      flood: () => L.esri.dynamicMapLayer({url: 'https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer', layers: [femaLayer], opacity: .5, minZoom: 11}),
      wetlands: () => L.esri.dynamicMapLayer({url: 'https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer', layers: [0],
        layerDefs: {0: "WETLAND_TYPE NOT IN ('Lake', 'Estuarine and Marine Deepwater', 'Estuarine and Marine Wetland')"}, opacity: .6, minZoom: 12}),
      zoning: () => L.layerGroup([
        regional(L.esri.featureLayer({url: CHARLES + 'Zoning/FeatureServer/0', minZoom: 10,
          style: f => { const g = (f.properties.GENZONE || '').toLowerCase();
            const c = g.includes('industr') ? '#8e44ad' : g.includes('commerc') ? '#e74c3c' : g.includes('resid') ? '#f1c40f' : '#27ae60';
            return {color: c, weight: 1, fillOpacity: .22}; }}).bindPopup(popup([['ZONING', 'Zone'], ['ZONE', 'Name'], ['GENZONE', 'General']])), CHARLES_CO),
        regional(L.esri.featureLayer({url: HSV + 'Zoning/ZoningDistricts/MapServer/5', minZoom: 10, simplifyFactor: .5, precision: 5, useCors: false,
          style: f => ({color: hsvZoneColor(f.properties.Zoning || ''), weight: 1, fillOpacity: .22})})
          .bindPopup(l => '<b>City of Huntsville zoning</b> ' + escapeHtml(l.feature.properties.Zoning || '')), HSV_AREA),
      ]),
      critical: () => L.esri.featureLayer({url: CHARLES + 'Charles_County_Chesapeake_Bay_Critical_Area_/FeatureServer/2', minZoom: 9,
        style: f => ({color: '#00838f', weight: 1, fillOpacity: f.properties.Designation === 'RCA' ? .3 : .12})}).bindPopup(popup([['Designation', 'Critical Area']])),
      rail: () => L.esri.featureLayer({url: NTAD + 'NTAD_North_American_Rail_Network_Lines/FeatureServer/0', where: "NET IN ('M','I','O','S','Y')", minZoom: 9,
        style: () => ({color: '#222', weight: 2.5, dashArray: '8 3'})}).bindPopup(popup([['RROWNER1', 'Railroad'], ['SUBDIV', 'Subdivision'], ['TRACKS', 'Tracks']])),
      military: () => L.esri.featureLayer({url: NTAD + 'NTAD_Military_Bases/FeatureServer/0', style: () => ({color: '#b30000', weight: 2, fillOpacity: .12})})
        .bindPopup(popup([['siteName', 'Installation'], ['siteReportingComponent', 'Branch'], ['featureDescription', 'Type']])),
      navy: () => L.layerGroup([
        L.esri.featureLayer({url: CHARLES + 'Military_Awareness_Zone/FeatureServer/0', style: () => ({color: '#b30000', weight: 2, dashArray: '4 4', fillOpacity: .05})}).bindPopup(() => 'Military Awareness Zone'),
        L.esri.featureLayer({url: CHARLES + 'Navy_Noise_Contours/FeatureServer/0', style: () => ({color: '#ff7f00', weight: 1, fillOpacity: .1})}).bindPopup(popup([['Descrip', 'Navy noise']])),
      ]),
      cities: () => L.layerGroup([
        regional(L.esri.dynamicMapLayer({url: 'https://feature.geographic.texas.gov/arcgis/rest/services/City_Boundaries/Texas_City_Boundaries/MapServer', opacity: .5}), TX),
        regional(L.esri.featureLayer({url: CHARLES + 'Incorporated_Towns/FeatureServer/0', where: "Inc_Muni <> 'Unincorporated'", style: () => ({color: '#ff00ff', weight: 2, fillOpacity: .05})}).bindPopup(popup([['Inc_Muni', 'Town']])), CHARLES_CO),
        regional(L.esri.featureLayer({url: TIGER_PLACES, where: "STATE = '01'", minZoom: 8, simplifyFactor: .5, precision: 5,
          style: () => ({color: '#ff00ff', weight: 2, fillOpacity: .05})}).bindPopup(popup([['NAME', 'City / town']])), ALABAMA),
      ]),
    };
    // Incentive programs, from the server's registry (crm/land/incentives.py).
    programs.forEach(g => {
      const pop = layer => {
        const props = layer.feature.properties || {};
        const area = (g.name || []).map(f => props[f]).filter(v => v != null && String(v).trim()).join(' · ');
        const contact = (g.contact || []).map(f => props[f]).filter(v => v != null && String(v).trim()).join(' · ');
        return `<b>${escapeHtml(g.label)}</b>${area ? '<br>' + escapeHtml((g.name_prefix || '') + area) : ''}`
          + `<div class="pop-note">${escapeHtml(g.summary)}</div>` + (contact ? `<div class="pop-note">Contact: ${escapeHtml(contact)}</div>` : '')
          + `<a href="${g.link}" target="_blank" rel="noopener">Program page ↗</a>`;
      };
      o['inc:' + g.key] = () => L.layerGroup([g.url, ...(g.extra_urls || [])].map(url => L.esri.featureLayer({
        url, where: g.where || '1=1', minZoom: g.geometry === 'point' ? 4 : 8, simplifyFactor: 0.6, precision: 5,
        pointToLayer: (f, ll) => L.circleMarker(ll, {radius: 8, color: '#fff', weight: 2, fillColor: g.color, fillOpacity: 1}),
        style: () => ({color: g.color, weight: 1.5, fillColor: g.color, fillOpacity: 0.16}),
      }).bindPopup(pop, {maxWidth: 320})));
    });
    return o;
  }

  // Display names, grouped, for layer pickers.
  const LABELS = [
    ['Power & energy', [['transmission', 'Transmission lines'], ['substations', 'Substations'], ['electric', 'Electric provider areas'], ['gas', 'Natural gas pipelines']]],
    ['Water & sewer', [['water', 'Water service areas'], ['sewer', 'Sewer service areas'], ['waterlines', 'Water & sewer lines (TX)']]],
    ['Site constraints', [['flood', 'FEMA flood zones'], ['wetlands', 'Wetlands, streams, ponds'], ['zoning', 'Zoning (Charles Co., Huntsville)'], ['critical', 'Bay Critical Area (Charles Co.)']]],
    ['Access & demand', [['rail', 'Rail'], ['military', 'Military installations'], ['navy', 'Navy zones (Charles Co.)'], ['cities', 'City / town limits']]],
    ['Parcels', [['lines', 'All parcel lines (zoom in)']]],
  ];

  window.LandLayers = {bases, overlays, LABELS, escapeHtml};
})();
