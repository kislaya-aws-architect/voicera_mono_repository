import { NextRequest, NextResponse } from "next/server"

import { SERVER_API_URL } from "@/lib/api-config"

const API_BASE_URL = SERVER_API_URL

type RouteContext = { params: Promise<{ reportId: string }> }

// PATCH - update a Sajag report's status, proxies to
// PATCH /api/v1/sajag/reports/{report_id}/status on the backend.
//
// NOTE: this only updates VoicERA's own record. It does not notify Glific or
// the citizen — see the PATCH handler in voicera_backend/app/routers/glific.py
// for why (open item "f" from the 9 Jul architecture note: the status-update-
// to-citizen mechanism is still undesigned).
export async function PATCH(request: NextRequest, context: RouteContext) {
  try {
    const authHeader = request.headers.get("Authorization")
    if (!authHeader) {
      return NextResponse.json(
        { error: "Authorization header is required" },
        { status: 401 }
      )
    }

    const { reportId } = await context.params
    const encodedReportId = encodeURIComponent(reportId)
    const body = await request.json()

    const response = await fetch(
      `${API_BASE_URL}/api/v1/sajag/reports/${encodedReportId}/status`,
      {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          Authorization: authHeader,
        },
        body: JSON.stringify(body),
      }
    )

    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status })
    }

    return NextResponse.json(data)
  } catch (error) {
    console.error("Error updating Sajag report status:", error)
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    )
  }
}
