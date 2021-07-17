# -*- coding: utf-8 -*-
"""
Skoda Connect integration

Read more at https://github.com/lendy007/homeassistant-skodaconnect/
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Union
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, SOURCE_REAUTH
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_RESOURCES,
    CONF_USERNAME, EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.icon import icon_for_battery_level
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from skodaconnect import Connection
from skodaconnect.vehicle import Vehicle

from .const import (
    PLATFORMS,
    CONF_MUTABLE,
    CONF_SCANDINAVIAN_MILES,
    CONF_SPIN,
    CONF_VEHICLE,
    CONF_UPDATE_INTERVAL,
    DATA,
    DATA_KEY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MIN_UPDATE_INTERVAL,
    SIGNAL_STATE_UPDATED,
    UNDO_UPDATE_LISTENER, UPDATE_CALLBACK, CONF_DEBUG, DEFAULT_DEBUG, CONF_CONVERT, CONF_NO_CONVERSION,
    CONF_IMPERIAL_UNITS,
    SERVICE_SET_SCHEDULE,
    SERVICE_SET_MAX_CURRENT,
    SERVICE_SET_CHARGE_LIMIT,
    SERVICE_SET_PHEATER_DURATION,
)
SERVICE_SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=32, max=32)),
        vol.Required("id", default=1): vol.In([1,2,3]),
        vol.Required("enabled", default=True): cv.boolean,
        vol.Required("recurring", default=False): cv.boolean,
        vol.Required("time", default="08:00"): cv.time,
        vol.Optional("date", default="2020-01-01"): cv.string,
        vol.Optional("days", default='nnnnnnn'): cv.string,
    }
)
SERVICE_SET_MAX_CURRENT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=32, max=32)),
        vol.Required("current"): vol.Any(vol.In(range(5, 33)), vol.In([ "maximum", "reduced"])),
    }
)
SERVICE_SET_CHARGE_LIMIT_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=32, max=32)),
        vol.Required("limit"): vol.In([0, 10, 20, 30, 40, 50]),
    }
)
SERVICE_SET_PHEATER_DURATION_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.string, vol.Length(min=32, max=32)),
        vol.Required("duration"): vol.In([10, 20, 30, 40, 50, 60]),
    }
)



_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Setup Skoda Connect component"""
    hass.data.setdefault(DOMAIN, {})

    if entry.options.get(CONF_UPDATE_INTERVAL):
        update_interval = timedelta(minutes=entry.options[CONF_UPDATE_INTERVAL])
    else:
        update_interval = timedelta(minutes=DEFAULT_UPDATE_INTERVAL)

    coordinator = SkodaCoordinator(hass, entry, update_interval)

    if not await coordinator.async_login():
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_REAUTH},
            data=entry,
        )
        return False

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_logout)

    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    data = SkodaData(entry.data, coordinator)
    instruments = coordinator.data

    def is_enabled(attr):
        """Return true if the user has enabled the resource."""
        return attr in entry.data.get(CONF_RESOURCES, [attr])

    components = set()
    for instrument in (
        instrument
        for instrument in instruments
        if instrument.component in PLATFORMS and is_enabled(instrument.slug_attr)
    ):
        data.instruments.add(instrument)
        components.add(PLATFORMS[instrument.component])

    for component in components:
        coordinator.platforms.append(component)
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    hass.data[DOMAIN][entry.entry_id] = {
        UPDATE_CALLBACK: update_callback,
        DATA: data,
        UNDO_UPDATE_LISTENER: entry.add_update_listener(_async_update_listener),
    }

    # Service functions
    async def get_car(service_call):
        dev_id = service_call.data.get("device_id")
        dev_reg = device_registry.async_get(hass)
        dev_entry = dev_reg.async_get(dev_id)

        # Get vehicle VIN from device identifiers
        skoda_identifiers = [
            identifier
            for identifier in dev_entry.identifiers
            if identifier[0] == DOMAIN
        ]
        vin_identifier = next(iter(skoda_identifiers))
        vin = vin_identifier[1]

        # Get the class object from the connection
        car = coordinator.connection.vehicle(vin)
        return car

    async def set_schedule(service_call=None):
        """Set departure schedule."""
        try:
            # Prepare data
            id = service_call.data.get("id", 0)
            try:
                time = service_call.data.get("time").strftime("%H:%M")
            except:
                time = "08:00"
            schedule = {
                "enabled": service_call.data.get("enabled"),
                "recurring": service_call.data.get("recurring"),
                "date": service_call.data.get("date"),
                "time": time,
                "days": service_call.data.get("days", "nnnnnnn")
            }

            # Find the correct car and execute service call
            car = await get_car(service_call)
            _LOGGER.info(f'Set departure schedule {id} with data {schedule} for car {car.vin}')
            if await car.set_timer_schedule(id, schedule):
                _LOGGER.info(f"Service call 'set_schedule' returned success!")
                await coordinator.async_request_refresh()
            else:
                _LOGGER.info(f"Failed to execute service call 'set_schedule' with data '{service_call}'")
        except Exception as e:
            raise

    async def set_charge_limit(service_call=None):
        """Set minimum charge limit."""
        try:
            car = await get_car(service_call)

            # Get charge limit and execute service call
            limit = service_call.data.get("limit", 50)
            if await car.set_charge_limit(limit):
                _LOGGER.info(f"Service call 'set_charge_limit' returned success!")
                await coordinator.async_request_refresh()
            else:
                _LOGGER.info(f"Failed to execute service call 'set_charge_limit' with data '{service_call}'")
        except Exception as e:
            raise

    async def set_current(service_call=None):
        """Set departure schedule."""
        try:
            # Find car from device id
            car = await get_car(service_call)

            # Get charge current and execute service call
            current = service_call.data.get('current', 'reduced')
            if current == "maximum":
                current = 254
            elif current == "reduced":
                current = 252

            if await car.set_charger_current(current):
                _LOGGER.info(f"Service call 'set_current' returned success!")
                await coordinator.async_request_refresh()
            else:
                _LOGGER.info(f"Failed to execute service call 'set_current' with data '{service_call}'")
        except Exception as e:
            raise

    async def set_pheater_duration(service_call=None):
        """Set duration for parking heater."""
        try:
            car = await get_car(service_call)
            car.pheater_duration = service_call.data.get("duration", car.pheater_duration)
            _LOGGER.info(f"Service call 'set_pheater_duration' succeeded!")
            await coordinator.async_request_refresh()
        except Exception as e:
            raise

    # Register entity service
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        set_schedule,
        schema = SERVICE_SET_SCHEDULE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_MAX_CURRENT,
        set_current,
        schema = SERVICE_SET_MAX_CURRENT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHARGE_LIMIT,
        set_charge_limit,
        schema = SERVICE_SET_CHARGE_LIMIT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PHEATER_DURATION,
        set_pheater_duration,
        schema = SERVICE_SET_PHEATER_DURATION_SCHEMA
    )

    return True


def update_callback(hass, coordinator):
    _LOGGER.debug("CALLBACK!")
    hass.async_create_task(
        coordinator.async_request_refresh()
    )


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]()

    return await async_unload_coordinator(hass, entry)


async def async_unload_coordinator(hass: HomeAssistant, entry: ConfigEntry):
    """Unload auth token based entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA].coordinator
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in coordinator.platforms
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def get_convert_conf(entry: ConfigEntry):
    return CONF_SCANDINAVIAN_MILES if entry.options.get(
        CONF_SCANDINAVIAN_MILES,
        entry.data.get(
            CONF_SCANDINAVIAN_MILES,
            False
        )
    ) else CONF_NO_CONVERSION


class SkodaData:
    """Hold component state."""

    def __init__(self, config, coordinator=None):
        """Initialize the component state."""
        self.vehicles = set()
        self.instruments = set()
        self.config = config.get(DOMAIN, config)
        self.names = self.config.get(CONF_NAME, None)
        self.coordinator = coordinator

    def instrument(self, vin, component, attr):
        """Return corresponding instrument."""
        return next(
            (
                instrument
                for instrument in (
                    self.coordinator.data
                    if self.coordinator is not None
                    else self.instruments
                )
                if instrument.vehicle.vin == vin
                and instrument.component == component
                and instrument.attr == attr
            ),
            None,
        )

    def vehicle_name(self, vehicle):
        """Provide a friendly name for a vehicle."""
        try:
            # Return name if already configured
            if isinstance(self.names, str):
                if len(self.names) > 0:
                    return self.names

            # Check if name already exists for VIN
            if vehicle.vin and vehicle.vin.lower() in self.names:
                return self.names[vehicle.vin.lower()]
        except:
            pass

        # Default name to nickname if supported, else vin number
        try:
            if vehicle.is_nickname_supported:
                return vehicle.nickname
            elif vehicle.vin:
                return vehicle.vin
        except:
            _LOGGER.info(f"Name set to blank")
            return ""


class SkodaEntity(Entity):
    """Base class for all Skoda entities."""

    def __init__(self, data, vin, component, attribute, callback=None):
        """Initialize the entity."""

        def update_callbacks():
            if callback is not None:
                callback(self.hass, data.coordinator)

        self.data = data
        self.vin = vin
        self.component = component
        self.attribute = attribute
        self.coordinator = data.coordinator
        self.instrument.callback = update_callbacks
        self.callback = callback

    async def async_update(self) -> None:
        """Update the entity.

        Only used by the generic entity update service.
        """

        # Ignore manual update requests if the entity is disabled
        if not self.enabled:
            return

        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self):
        """Register update dispatcher."""
        if self.coordinator is not None:
            self.async_on_remove(
                self.coordinator.async_add_listener(self.async_write_ha_state)
            )
        else:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, SIGNAL_STATE_UPDATED, self.async_write_ha_state
                )
            )

    @property
    def instrument(self):
        """Return corresponding instrument."""
        return self.data.instrument(self.vin, self.component, self.attribute)

    @property
    def icon(self):
        """Return the icon."""
        if self.instrument.attr in ["battery_level", "charging"]:
            return icon_for_battery_level(
                battery_level=self.instrument.state, charging=self.vehicle.charging
            )
        else:
            return self.instrument.icon

    @property
    def vehicle(self):
        """Return vehicle."""
        return self.instrument.vehicle

    @property
    def _entity_name(self):
        return self.instrument.name

    @property
    def _vehicle_name(self):
        return self.data.vehicle_name(self.vehicle)

    @property
    def name(self):
        """Return full name of the entity."""
        return f"{self._vehicle_name} {self._entity_name}"

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def assumed_state(self):
        """Return true if unable to access real state of entity."""
        return True

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attributes = dict(
            self.instrument.attributes,
            model=f"{self.vehicle.model}/{self.vehicle.model_year}",
        )

        if not self.vehicle.is_model_image_supported:
            return attributes

        attributes["image_url"] = self.vehicle.model_image
        return attributes

    @property
    def device_info(self):
        """Return the device_info of the device."""
        return {
            "identifiers": {(DOMAIN, self.vin)},
            "name": self._vehicle_name,
            "manufacturer": "Skoda",
            "model": self.vehicle.model,
            "sw_version": self.vehicle.model_year,
        }

    @property
    def available(self):
        """Return if sensor is available."""
        if self.data.coordinator is not None:
            return self.data.coordinator.last_update_success
        return True

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return f"{self.vin}-{self.component}-{self.attribute}"


class SkodaCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, entry, update_interval: timedelta):
        self.vin = entry.data[CONF_VEHICLE].upper()
        self.entry = entry
        self.platforms = []
        self.report_last_updated = None
        self.connection = Connection(
            session=async_get_clientsession(hass),
            username=self.entry.data[CONF_USERNAME],
            password=self.entry.data[CONF_PASSWORD],
            fulldebug=self.entry.options.get(CONF_DEBUG, self.entry.data.get(CONF_DEBUG, DEFAULT_DEBUG)),
        )

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self):
        """Update data via library."""
        vehicle = await self.update()

        if not vehicle:
            raise UpdateFailed("Failed to update Connect. Need to accept EULA? Try logging in to the portal: https://www.skoda-connect.com/")

        # Backward compatibility
        default_convert_conf = get_convert_conf(self.entry)

        convert_conf = self.entry.options.get(
            CONF_CONVERT,
            self.entry.data.get(
                CONF_CONVERT,
                default_convert_conf
            )
        )

        dashboard = vehicle.dashboard(
            mutable=self.entry.data.get(CONF_MUTABLE),
            spin=self.entry.data.get(CONF_SPIN),
            miles=convert_conf == CONF_IMPERIAL_UNITS,
            scandinavian_miles=convert_conf == CONF_SCANDINAVIAN_MILES,
        )

        return dashboard.instruments

    async def async_logout(self):
        """Logout from Skoda Connect"""
        try:
            if self.connection.logged_in:
                await self.connection.logout()
        except Exception as ex:
            _LOGGER.error("Could not log out from Skoda Connect, %s", ex)
            return False
        return True

    async def async_login(self):
        """Login to Skoda Connect"""
        # check if we can login
        if not self.connection.logged_in:
            await self.connection.doLogin()
            if not self.connection.logged_in:
                _LOGGER.warning(
                    "Could not login to Skoda Connect, please check your credentials and verify that the service is working"
                )
                return False

        return True

    async def update(self) -> Union[bool, Vehicle]:
        """Update status from Skoda Connect"""

        # Update vehicles
        if not await self.connection.update():
            _LOGGER.warning("Could not query update from Skoda Connect")
            return False

        _LOGGER.debug("Updating data from Skoda Connect")
        for vehicle in self.connection.vehicles:
            if vehicle.vin.upper() == self.vin:
                return vehicle

        return False
