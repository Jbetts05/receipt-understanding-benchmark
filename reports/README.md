# Aggregate benchmark reports

Final aggregate JSON, CSV, and HTML reports can be written here with:

```powershell
receipt-bench report `
  --config configs\solutions.yaml `
  --run-id <run-id> `
  --output reports\<run-id>
```

Only aggregate measurements belong in Git. Raw provider responses, receipt
images, normalized SROIE labels, and per-receipt results remain in ignored local
directories.
