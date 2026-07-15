"use client"

import { useCallback, useEffect, useState } from "react"
import { format } from "date-fns"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import {
  AlertCircle,
  ExternalLink,
  Loader2,
  MapPin,
  RefreshCw,
  ShieldAlert,
} from "lucide-react"
import {
  getSajagReports,
  updateSajagReportStatus,
  SAJAG_REPORT_STATUSES,
  type SajagReport,
  type SajagReportStatus,
} from "@/lib/api"

// SLF brand accent, per the Sajag concept note (Section 6, Design Principles):
// "Future UI aligns to SLF brand (deep red #CE2127, not orange)". Used only as
// an accent within this Sajag-specific view — the rest of the VoicERA admin
// shell (sidebar, other pages) is unaffected.
const SLF_RED = "#CE2127"

const STATUS_BADGE_STYLE: Record<SajagReportStatus, string> = {
  Received: "bg-slate-100 text-slate-700 border-slate-200",
  Triaged: "bg-blue-50 text-blue-700 border-blue-200",
  Validated: "bg-indigo-50 text-indigo-700 border-indigo-200",
  Escalated: "bg-amber-50 text-amber-800 border-amber-200",
  "In-Progress": "bg-purple-50 text-purple-700 border-purple-200",
  Resolved: "bg-emerald-50 text-emerald-700 border-emerald-200",
  "Feedback-Sent": "bg-teal-50 text-teal-700 border-teal-200",
}

const TIER_BADGE_STYLE: Record<string, string> = {
  Confirmed: "bg-red-50 text-red-700 border-red-200",
  Emerging: "bg-orange-50 text-orange-700 border-orange-200",
  Contextual: "bg-slate-50 text-slate-600 border-slate-200",
}

const formatTimestamp = (value?: string | null): string => {
  if (!value) return "—"
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return "—"
  return format(parsed, "dd/MM/yyyy, hh:mm a")
}

const truncate = (value: string | null | undefined, max: number): string => {
  if (!value) return "—"
  return value.length > max ? `${value.slice(0, max)}…` : value
}

export default function SajagReportsPage() {
  const [reports, setReports] = useState<SajagReport[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>("all")
  const [selectedReport, setSelectedReport] = useState<SajagReport | null>(null)
  const [pendingStatus, setPendingStatus] = useState<SajagReportStatus | "">("")
  const [statusNote, setStatusNote] = useState("")
  const [isUpdatingStatus, setIsUpdatingStatus] = useState(false)
  const [updateError, setUpdateError] = useState<string | null>(null)

  const loadReports = useCallback(async () => {
    setIsLoading(true)
    setLoadError(null)
    try {
      const rows = await getSajagReports(
        statusFilter === "all" ? undefined : statusFilter
      )
      setReports(rows)
    } catch (error) {
      setLoadError(
        error instanceof Error ? error.message : "Failed to load Sajag reports"
      )
    } finally {
      setIsLoading(false)
    }
  }, [statusFilter])

  useEffect(() => {
    loadReports()
  }, [loadReports])

  const openReport = (report: SajagReport) => {
    setSelectedReport(report)
    setPendingStatus(report.status)
    setStatusNote("")
    setUpdateError(null)
  }

  const submitStatusUpdate = async () => {
    if (!selectedReport || !pendingStatus) return
    setIsUpdatingStatus(true)
    setUpdateError(null)
    try {
      const updated = await updateSajagReportStatus(
        selectedReport.report_id,
        pendingStatus,
        statusNote || undefined
      )
      setSelectedReport(updated)
      setReports((prev) =>
        prev.map((r) => (r.report_id === updated.report_id ? updated : r))
      )
    } catch (error) {
      setUpdateError(
        error instanceof Error ? error.message : "Failed to update status"
      )
    } finally {
      setIsUpdatingStatus(false)
    }
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5" style={{ color: SLF_RED }} />
            <h1 className="text-2xl font-semibold tracking-tight">
              Sajag Reports
            </h1>
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            Community-reported road hazards via SaveLIFE Foundation&apos;s WhatsApp
            bot (Glific), routed through VoicERA.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="Filter by status" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {SAJAG_REPORT_STATUSES.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="outline" size="icon" onClick={loadReports} disabled={isLoading}>
            <RefreshCw className={isLoading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {isLoading ? "Loading…" : `${reports.length} report${reports.length === 1 ? "" : "s"}`}
          </CardTitle>
          <CardDescription>
            Triangulation tier is shown once it&apos;s populated — that scoring
            step is not yet wired up (open item pending confirmation with SLF on
            whether it runs here or on their side).
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loadError && (
            <div className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive mb-4">
              <AlertCircle className="h-4 w-4 shrink-0" />
              {loadError}
            </div>
          )}

          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin mr-2" />
              Loading reports…
            </div>
          ) : reports.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-16 text-center text-muted-foreground">
              <MapPin className="h-8 w-8 mb-1 opacity-50" />
              <p className="font-medium text-foreground">No reports yet</p>
              <p className="text-sm max-w-md">
                Reports appear here once the Glific webhook receives a real
                submission, or once demo data has been loaded for a walkthrough.
                See <code className="text-xs bg-muted px-1 py-0.5 rounded">
                  voicera_backend/scripts/seed_sajag_demo_data.py
                </code> if you&apos;re setting this up as a standalone PoC.
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Received</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Tier</TableHead>
                  <TableHead>Report</TableHead>
                  <TableHead>Location</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {reports.map((report) => (
                  <TableRow key={report.report_id}>
                    <TableCell className="whitespace-nowrap text-sm">
                      {formatTimestamp(report.received_at)}
                    </TableCell>
                    <TableCell>
                      <Badge
                        variant="outline"
                        className={STATUS_BADGE_STYLE[report.status] ?? ""}
                      >
                        {report.status}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {report.triangulation_tier ? (
                        <Badge
                          variant="outline"
                          className={TIER_BADGE_STYLE[report.triangulation_tier] ?? ""}
                        >
                          {report.triangulation_tier}
                        </Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">Not scored</span>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[320px] text-sm">
                      {truncate(report.transcription, 90)}
                      {report.hazard_tags && report.hazard_tags.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {report.hazard_tags.map((tag) => (
                            <Badge key={tag} variant="secondary" className="text-[10px]">
                              {tag.replace(/_/g, " ")}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-sm">
                      {report.latitude != null && report.longitude != null ? (
                        <a
                          href={`https://www.google.com/maps?q=${report.latitude},${report.longitude}`}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                        >
                          <MapPin className="h-3.5 w-3.5" />
                          {report.latitude.toFixed(4)}, {report.longitude.toFixed(4)}
                          <ExternalLink className="h-3 w-3" />
                        </a>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button variant="outline" size="sm" onClick={() => openReport(report)}>
                        View
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={!!selectedReport} onOpenChange={(open) => !open && setSelectedReport(null)}>
        <DialogContent className="max-w-lg">
          {selectedReport && (
            <>
              <DialogHeader>
                <DialogTitle>Report {selectedReport.report_id.slice(0, 8)}</DialogTitle>
                <DialogDescription>
                  Received {formatTimestamp(selectedReport.received_at)} via{" "}
                  {selectedReport.channel}
                </DialogDescription>
              </DialogHeader>

              <div className="space-y-4 text-sm">
                <div>
                  <p className="font-medium text-muted-foreground mb-1">Report text</p>
                  <p className="rounded-md bg-muted p-3">
                    {selectedReport.transcription || "No transcription available yet."}
                  </p>
                </div>

                {selectedReport.hazard_tags && selectedReport.hazard_tags.length > 0 && (
                  <div>
                    <p className="font-medium text-muted-foreground mb-1">Hazard tags</p>
                    <div className="flex flex-wrap gap-1">
                      {selectedReport.hazard_tags.map((tag) => (
                        <Badge key={tag} variant="secondary">
                          {tag.replace(/_/g, " ")}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                {selectedReport.photo_url && (
                  <div>
                    <p className="font-medium text-muted-foreground mb-1">Photo</p>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={selectedReport.photo_url}
                      alt="Reported hazard"
                      className="rounded-md border max-h-64 object-cover"
                    />
                    <p className="text-xs text-amber-700 mt-1">
                      Not yet redacted — face/number-plate redaction is unbuilt (see
                      status doc). Do not share this image externally as-is.
                    </p>
                  </div>
                )}

                {selectedReport.latitude != null && selectedReport.longitude != null && (
                  <div>
                    <p className="font-medium text-muted-foreground mb-1">Location</p>
                    <a
                      href={`https://www.google.com/maps?q=${selectedReport.latitude},${selectedReport.longitude}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-blue-600 hover:underline"
                    >
                      <MapPin className="h-3.5 w-3.5" />
                      {selectedReport.latitude.toFixed(5)}, {selectedReport.longitude.toFixed(5)}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  </div>
                )}

                <div className="grid grid-cols-2 gap-3 text-xs text-muted-foreground">
                  <div>
                    <span className="block font-medium text-foreground">Reporter (hashed)</span>
                    {selectedReport.contact_phone_hash.slice(0, 16)}…
                  </div>
                  <div>
                    <span className="block font-medium text-foreground">Language</span>
                    {selectedReport.language_id || "—"}
                  </div>
                </div>

                <div className="border-t pt-4">
                  <p className="font-medium text-muted-foreground mb-2">Update status</p>
                  <div className="flex items-center gap-2">
                    <Select
                      value={pendingStatus}
                      onValueChange={(v) => setPendingStatus(v as SajagReportStatus)}
                    >
                      <SelectTrigger className="w-[180px]">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {SAJAG_REPORT_STATUSES.map((s) => (
                          <SelectItem key={s} value={s}>
                            {s}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Button
                      onClick={submitStatusUpdate}
                      disabled={isUpdatingStatus || pendingStatus === selectedReport.status}
                    >
                      {isUpdatingStatus && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
                      Update
                    </Button>
                  </div>
                  <Textarea
                    className="mt-2"
                    placeholder="Optional note (e.g. what was validated, who it was escalated to)"
                    value={statusNote}
                    onChange={(e) => setStatusNote(e.target.value)}
                    rows={2}
                  />
                  <p className="text-xs text-amber-700 mt-2">
                    This updates VoicERA&apos;s own record only — it does not notify
                    Glific or the citizen. Closing the loop back to the reporter is
                    not yet implemented (see status doc).
                  </p>
                  {updateError && (
                    <p className="text-xs text-destructive mt-2">{updateError}</p>
                  )}
                </div>
              </div>

              <DialogFooter>
                <Button variant="outline" onClick={() => setSelectedReport(null)}>
                  Close
                </Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
