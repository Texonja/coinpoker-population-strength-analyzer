# Sample data

The repository includes two public test artifacts:

```text
../cash_example.txt
coinpoker_population_strength_sample_pl25.zip
```

`cash_example.txt` contains 10,000 sanitized CoinPoker hands with a standardized Hero name, synthetic hand/table IDs, and shifted timestamps.

The ZIP contains a report generated from that public sample with the PL25 filter. It is safe to inspect without running the analyzer.

For another public sample, run `make_public_sample.bat` and review the generated text file manually before committing it.
