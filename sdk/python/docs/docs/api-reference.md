# API Reference

::: trendstorm_sdk.TrendStormClient
    options:
      show_source: false

::: trendstorm_sdk.SyncTrendStormClient
    options:
      show_source: false

## Resources

::: trendstorm_sdk.resources.categories.CategoriesResource
    options:
      show_source: false

::: trendstorm_sdk.resources.sources.SourcesResource
    options:
      show_source: false

::: trendstorm_sdk.resources.jobs.JobsResource
    options:
      show_source: false

::: trendstorm_sdk.resources.reviews.ReviewsResource
    options:
      show_source: false

::: trendstorm_sdk.resources.quota.QuotaResource
    options:
      show_source: false

::: trendstorm_sdk.resources.api_keys.ApiKeysResource
    options:
      show_source: false

## Errors

::: trendstorm_sdk._errors
    options:
      show_source: false
      members:
        - TrendStormError
        - ConfigurationError
        - StreamError
        - HeartbeatTimeout
        - APIError
        - RateLimited
        - NotFound
        - Unauthorized
        - ValidationError
        - ServerError
