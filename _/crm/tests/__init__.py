import time

from crm.land import sources

# Pages with the flood layer look its id up at FEMA; tests never wait on the network for it.
sources._fema_layer_cache["id"] = (sources.FEMA_FLOOD_LAYER_DEFAULT, time.time() + 10 * 365 * 86400)
